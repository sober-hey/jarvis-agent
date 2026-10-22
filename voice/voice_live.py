"""Opt-in local live voice; no fixed speech, retained recordings, or cloud ASR.

Run from the repository: .venv/bin/python packaging/voice_live.py --url http://127.0.0.1:8765
Enter arms one utterance (Enter again ends it); --mode always explicitly opts into
always-on energy segmentation. Ctrl-C exits. Playback is interruptible in either mode.
Requires an already running local JARVIS web service, ffmpeg AVFoundation and afplay.
Only model replies reach TTS. Errors are diagnostic logs, never speech.
Inputs are resolved by unique case-insensitive name substring at startup; omitted
--device prefers MacBook/BuiltIn inputs. --device default explicitly follows the OS.
Selected device name/index are logged; --status prints them without recording.
For acceptance only, --player / JARVIS_VOICE_PLAYER overrides afplay with shell-free
argv: {audio} is replaced by the file path (or the path is appended when absent).
Example: --player 'ffmpeg -nostdin -i {audio} -f audiotoolbox
-audio_device_index <freshly-enumerated-built-in-speaker-index> -y /dev/null'.
This routes acceptance replies without changing the system default output.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import voice

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms balances onset detection against polling overhead.
FRAME_BYTES = FRAME_SAMPLES * 2
# Below the measured 0.010 loopback speech, but above ~0.00066 room noise.
ADAPTIVE_MIN_THRESHOLD = 0.002
# 4x RMS (~12 dB) gives noise headroom without requiring loud speech.
ADAPTIVE_NOISE_MULTIPLIER = 4.0
# Five seconds absorbs short fluctuations; the low fifth rejects occasional taps.
NOISE_WINDOW_SECONDS = 5.0
NOISE_QUANTILE = 0.20


def self_evaluate() -> dict[str, Any]:
    """Describe the opt-in cascade; never enumerate, capture, play or send data."""
    from datetime import UTC, datetime

    return {
        "module": "voice_live",
        "domain": "opt-in local live voice segmentation, turn cancellation and playback",
        "current_version": "cascaded RMS segmentation -> local ASR -> loopback timeline -> sentence TTS; not a duplex speech model",
        "known_limitations": [
            "The CLI requires macOS AVFoundation, ffmpeg and a player; it does not supply a cross-platform capture backend.",
            "Segmentation and barge-in use RMS energy thresholds with a playback multiplier and echo tail, not acoustic echo cancellation or a speech classifier.",
            "ASR, timeline HTTP and TTS calls are blocking; cancellation suppresses late output but cannot cancel the server-side model or running synthesis.",
            "The timeline endpoint returns a complete reply before sentence synthesis starts; model tokens are not streamed to TTS.",
            "Disarmed push mode still inspects microphone PCM during playback for barge-in; it is not a physical microphone mute or wake-word detector.",
        ],
        "improvement_directions": [
            "Evaluate an injected full-duplex speech backend at the existing voice port rather than treating this cascade as duplex.",
            "Add explicitly opted-in acoustic echo cancellation and speech/non-speech discrimination with synthetic regressions.",
            "Introduce cancellable turn APIs and bounded streaming reply delivery while keeping uncertain HTTP outcomes non-retriable.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


@dataclass(frozen=True)
class LiveConfig:
    # None estimates room noise; an explicit normalized PCM16 RMS fixes the base.
    energy_threshold: float | None = None
    playback_multiplier: float = 3.0  # +9.5 dB rejects quiet speaker leakage.
    onset_seconds: float = 0.12  # Six frames reject clicks; also bounds barge-in delay.
    silence_seconds: float = 0.60  # Allows short intra-sentence pauses in Mandarin.
    min_speech_seconds: float = 0.24  # Avoid submitting isolated taps/noise to ASR.
    max_utterance_seconds: float = 30.0  # Bounds memory and matches voice ASR chunk cap.
    preroll_seconds: float = 0.20  # Retain initial consonants before onset confirmation.
    echo_tail_seconds: float = 0.20  # Keep raised threshold over short room reverberation.

    def __post_init__(self) -> None:
        values = (
            self.playback_multiplier,
            self.onset_seconds,
            self.silence_seconds,
            self.min_speech_seconds,
            self.max_utterance_seconds,
            self.preroll_seconds,
            self.echo_tail_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("live config values must be positive and finite")
        if self.energy_threshold is not None and (
            not math.isfinite(self.energy_threshold) or not 0 < self.energy_threshold < 1
        ):
            raise ValueError("invalid energy threshold")
        if self.playback_multiplier < 1:
            raise ValueError("invalid energy thresholds")
        if not self.onset_seconds <= self.min_speech_seconds <= self.max_utterance_seconds:
            raise ValueError("invalid speech bounds")
        if self.max_utterance_seconds > 30 or self.preroll_seconds > 1:
            raise ValueError("audio buffer bounds exceeded")


def pcm_rms(pcm: bytes) -> float:
    if len(pcm) != FRAME_BYTES:
        raise ValueError("expected one 20ms mono PCM16 frame")
    samples = struct.unpack(f"<{FRAME_SAMPLES}h", pcm)
    return math.sqrt(sum(value * value for value in samples) / FRAME_SAMPLES) / 32768


@dataclass(frozen=True)
class AudioEvent:
    started: bool = False
    utterance: bytes | None = None
    ended: bool = False
    trailing_silence_seconds: float = 0.0


class LiveError(RuntimeError):
    """Sanitized diagnostic: no audio, transcript, model reply, or provider payload."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise LiveError("timeline redirect refused")


class TimelineClient:
    """Exactly one local POST per utterance; uncertain outcomes are never retried.

    Existing /api/timeline/chat returns a complete model reply, not streamed tokens.
    Sentence TTS starts as soon as that reply is available.
    """

    def __init__(self, url: str, timeout: float = 360.0) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("timeline URL must be a loopback HTTP origin")
        if not math.isfinite(timeout) or not 0 < timeout <= 900:
            raise ValueError("invalid timeline timeout")
        # Accessing port also validates malformed/out-of-range URL ports.
        _ = parsed.port
        self.origin = url.rstrip("/")
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def reply(self, text: str) -> str:
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 16000:
            raise LiveError("invalid transcription length")
        payload = {"prompt": text, "attempt_id": str(uuid.uuid4()), "source_channel": "voice"}
        request = Request(
            self.origin + "/api/timeline/chat",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json", "Origin": self.origin},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise LiveError("timeline outcome uncertain; no retry")
                blob = response.read(262145)
                if len(blob) > 262144:
                    raise LiveError("timeline response too large")
            raw: object = json.loads(blob)
            if not isinstance(raw, dict):
                raise LiveError("invalid timeline result")
            reply = cast(dict[str, object], raw).get("text")
            if not isinstance(reply, str) or not reply.strip():
                raise LiveError("timeline returned no reply")
            return reply
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise LiveError(f"timeline request failed ({type(exc).__name__}); no retry") from None


class Player(Protocol):
    @property
    def playing(self) -> bool: ...

    def start(self, path: Path) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class TurnTiming:
    asr_seconds: float
    model_seconds: float
    first_play_start_seconds: float
    interrupted: bool


class TurnPipeline:
    """One bounded worker + one latest pending utterance. Capture never waits on it.

    interrupt serializes against playback start and drops all not-yet-played output.
    The existing HTTP/ASR/TTS APIs are blocking: cancellation suppresses their result,
    not a server-side model operation or a running say synthesis. No HTTP retry.
    """

    def __init__(
        self,
        asr: Callable[[Path], str],
        reply: Callable[[str], str],
        tts: Callable[[str, Path], Iterator[Path]],
        player: Player,
    ) -> None:
        self._asr, self._reply, self._tts, self.player = asr, reply, tts, player
        self._condition = threading.Condition(threading.RLock())
        self._pending: tuple[bytes, float, threading.Event] | None = None
        self._cancel: threading.Event | None = None
        self._working = False
        self._closed = False
        self._audio_directory: Path | None = None
        self.errors: list[str] = []
        self.timings: list[TurnTiming] = []
        self._thread = threading.Thread(target=self._work, name="jarvis-voice-turn", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._working or self._pending is not None

    @property
    def playing(self) -> bool:
        with self._condition:
            return self.player.playing

    def interrupt(self) -> None:
        with self._condition:
            if self._cancel is not None:
                self._cancel.set()
            if self._pending is not None:
                self._pending[2].set()
                self._pending = None
            self.player.stop()

    def submit(self, pcm: bytes, ended_at: float | None = None) -> None:
        if not pcm or len(pcm) % 2 or len(pcm) > SAMPLE_RATE * 2 * 31:
            raise ValueError("invalid utterance PCM bounds")
        with self._condition:
            if self._closed:
                raise LiveError("pipeline is closed")
            self.interrupt()
            self._pending = (
                pcm,
                ended_at if ended_at is not None else time.monotonic(),
                threading.Event(),
            )
            self._condition.notify_all()

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._closed)
                if self._closed:
                    return
                task = self._pending
                self._pending = None
                if task is None:
                    continue
                self._cancel = task[2]
                self._working = True
            try:
                self._run_turn(*task)
            except Exception as exc:
                message = f"voice turn failed ({type(exc).__name__}); payload suppressed; no retry"
                self.errors.append(message)
                logging.error(message)
            finally:
                with self._condition:
                    self.player.stop()
                    self._working = False
                    self._cancel = None
                    self._condition.notify_all()

    def _run_turn(self, pcm: bytes, ended_at: float, cancel: threading.Event) -> None:
        stream: Iterator[Path] | None = None
        asr_seconds = model_seconds = 0.0
        first_start = -1.0
        with tempfile.TemporaryDirectory(prefix="jarvis-voice-live-") as root:
            directory = Path(root)
            recording = directory / "input.wav"
            with self._condition:
                if cancel.is_set() or self._closed:
                    return
                self._audio_directory = directory
                with wave.open(str(recording), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(SAMPLE_RATE)
                    output.writeframes(pcm)
            try:
                if cancel.is_set():
                    return
                began = time.monotonic()
                text = self._asr(recording)
                asr_seconds = time.monotonic() - began
                recording.unlink(missing_ok=True)
                if cancel.is_set() or not text.strip():
                    return
                began = time.monotonic()
                response = self._reply(text)
                model_seconds = time.monotonic() - began
                if cancel.is_set() or not response.strip():
                    return
                stream = self._tts(response, directory / "reply")
                while not cancel.is_set():
                    try:
                        chunk = next(stream)
                    except StopIteration:
                        break
                    # next(stream) can block on synthesis: recheck before any sound.
                    with self._condition:
                        if cancel.is_set() or self._closed:
                            break
                        self.player.start(chunk)
                        if first_start < 0:
                            first_start = time.monotonic() - ended_at
                    # Wait interruptibly; never ask for sentence two before first starts.
                    while not cancel.is_set() and self.playing:
                        cancel.wait(0.01)
                    chunk.unlink(missing_ok=True)
                    if cancel.is_set():
                        break
            finally:
                if stream is not None:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                with self._condition:
                    self.player.stop()
                    self._audio_directory = None
                self.timings.append(
                    TurnTiming(asr_seconds, model_seconds, first_start, cancel.is_set())
                )

    def close(self, timeout: float = 2.0) -> bool:
        with self._condition:
            self.interrupt()
            self._closed = True
            # Removing our private directory on macOS also unlinks open input files;
            # a blocked backend may still hold its FD, but no recording is retained.
            if self._audio_directory is not None:
                shutil.rmtree(self._audio_directory, ignore_errors=True)
            self._condition.notify_all()
        self._thread.join(timeout)
        return not self._thread.is_alive()


class EnergySegmenter:
    def __init__(self, config: LiveConfig) -> None:
        self.config = config
        self._pre: deque[bytes] = deque(maxlen=max(1, math.ceil(config.preroll_seconds / 0.02)))
        self._active = False
        self._frames: list[bytes] = []
        self._onset = 0
        self._speech = 0
        self._silence = 0
        self._tail = 0
        self._noise: deque[float] = deque(maxlen=math.ceil(NOISE_WINDOW_SECONDS / 0.02))
        self.noise_floor: float | None = None

    @property
    def base_threshold(self) -> float:
        if self.config.energy_threshold is not None:
            return self.config.energy_threshold
        return max(ADAPTIVE_MIN_THRESHOLD, (self.noise_floor or 0.0) * ADAPTIVE_NOISE_MULTIPLIER)

    def status(self, *, playback: bool = False) -> dict[str, str | float | None]:
        return {
            "threshold_mode": "fixed" if self.config.energy_threshold is not None else "adaptive",
            "noise_floor": self.noise_floor,
            "energy_threshold": self.base_threshold
            * (self.config.playback_multiplier if playback or self._tail else 1.0),
        }

    def reset(self) -> None:
        self._pre.clear()
        self._active = False
        self._frames.clear()
        self._onset = self._speech = self._silence = 0

    def finish(self) -> bytes | None:
        result = (
            b"".join(self._frames)
            if self._speech * 0.02 >= self.config.min_speech_seconds
            else None
        )
        self.reset()
        return result

    def feed(self, pcm: bytes, *, enabled: bool = True, playback: bool = False) -> AudioEvent:
        energy = pcm_rms(pcm)
        if playback:
            self._tail = math.ceil(self.config.echo_tail_seconds / 0.02)
        elif self._tail:
            self._tail -= 1
        threshold = self.base_threshold * (
            self.config.playback_multiplier if playback or self._tail else 1.0
        )
        # Use only idle, sub-threshold frames. Never learn speech onset, active
        # utterances, or our speaker/reverb as room noise. Retain this window across
        # utterances and disarmed push periods so room changes continue to adapt.
        if not self._active and not playback and not self._tail and energy < self.base_threshold:
            self._noise.append(energy)
            ordered = sorted(self._noise)
            self.noise_floor = ordered[int((len(ordered) - 1) * NOISE_QUANTILE)]
        if not enabled:
            self.reset()
            return AudioEvent()
        loud = energy >= threshold
        if not self._active:
            self._pre.append(pcm)
            self._onset = self._onset + 1 if loud else 0
            if self._onset * 0.02 < self.config.onset_seconds:
                return AudioEvent()
            self._active = True
            self._frames = list(self._pre)
            self._speech = self._onset
            return AudioEvent(started=True)
        self._frames.append(pcm)
        self._speech += int(loud)
        self._silence = 0 if loud else self._silence + 1
        if (
            self._silence * 0.02 >= self.config.silence_seconds
            or len(self._frames) * 0.02 >= self.config.max_utterance_seconds
        ):
            trailing = self._silence * 0.02
            return AudioEvent(
                utterance=self.finish(), ended=True, trailing_silence_seconds=trailing
            )
        return AudioEvent()


class CaptureError(LiveError):
    """Capture cannot continue; callers must stop, never automatically reopen it."""


@dataclass(frozen=True)
class AudioDevice:
    index: str | None
    name: str

    @property
    def selector(self) -> str:
        return self.index if self.index is not None else "default"


def parse_audio_devices(listing: str) -> tuple[AudioDevice, ...]:
    """Parse only the audio section; video devices have an independent index space."""
    audio = False
    devices: list[AudioDevice] = []
    for line in listing.splitlines():
        if "AVFoundation audio devices:" in line:
            audio = True
            continue
        if "AVFoundation video devices:" in line:
            audio = False
        if audio and (match := re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)):
            devices.append(AudioDevice(match[1], match[2]))
    return tuple(devices)


def select_audio_device(devices: tuple[AudioDevice, ...], query: str | None = None) -> AudioDevice:
    """Resolve names at startup; numeric selectors are deliberately not supported."""
    if query == "default":
        return AudioDevice(None, "system default input (name unavailable)")
    if query is not None:
        if not query.strip() or any(char in query for char in "\n\r\0"):
            raise ValueError("invalid AVFoundation device name")
        matches = [device for device in devices if query.casefold() in device.name.casefold()]
        if len(matches) != 1:
            raise CaptureError(f"no unique audio device name match ({len(matches)} matches)")
        return matches[0]
    builtin = [
        device
        for device in devices
        if any(token in device.name.casefold() for token in ("macbook", "builtin", "built-in"))
    ]
    if len(builtin) > 1:
        raise CaptureError("ambiguous built-in microphones; specify a unique device name")
    return builtin[0] if builtin else AudioDevice(None, "system default input (name unavailable)")


def list_audio_devices(executable: str) -> tuple[AudioDevice, ...]:
    try:
        result = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-nostdin",
                "-f",
                "avfoundation",
                "-list_devices",
                "true",
                "-i",
                "",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CaptureError(f"audio device enumeration failed ({type(exc).__name__})") from None
    # FFmpeg normally exits nonzero after printing this diagnostic list.
    devices = parse_audio_devices(result.stderr)
    if not devices:
        raise CaptureError("audio device enumeration returned no inputs; check app permission")
    return devices


class ZeroAudioError(CaptureError):
    """Frames arrived but the selected device delivered only exact digital zeros."""


class AudioCapture(Protocol):
    def frames(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


def _spawn_capture(argv: list[str]) -> subprocess.Popen[bytes]:
    # stderr is discarded: AVFoundation may expose private device names there.
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )


class FFmpegCapture:
    """Continuous mono 16k PCM, bounded frames, single attempt, bounded startup.

    No frames for five seconds (including a macOS permission prompt) stops the
    child. Five seconds of exact digital zeros raises ZeroAudioError separately.
    Names are enumerated on construction; indices are never persisted across runs.
    """

    def __init__(
        self,
        executable: str,
        device: str | None = None,
        *,
        startup_timeout: float = 5.0,
        zero_audio_seconds: float = 5.0,
        factory: Callable[[list[str]], subprocess.Popen[bytes]] = _spawn_capture,
    ) -> None:
        self.selected_device = select_audio_device(list_audio_devices(executable), device)
        if not math.isfinite(startup_timeout) or not 0.01 <= startup_timeout <= 30:
            raise ValueError("invalid capture timeout")
        if not math.isfinite(zero_audio_seconds) or not 0.02 <= zero_audio_seconds <= 30:
            raise ValueError("invalid zero-audio timeout")
        self._zero_limit = math.ceil(zero_audio_seconds * SAMPLE_RATE / FRAME_SAMPLES)
        self._argv = [
            executable,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-i",
            ":" + self.selected_device.selector,
            "-vn",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            "-flush_packets",
            "1",  # Avoid AVIO packet batching delaying 20ms capture delivery.
            "pipe:1",
        ]
        self._factory = factory
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = threading.Event()
        self._started = False
        self._timeout = startup_timeout

    def status(self) -> dict[str, str | None]:
        return {
            "device_name": self.selected_device.name,
            "device_index": self.selected_device.index,
            "device_selector": self.selected_device.selector,
        }

    def frames(self) -> Iterator[bytes]:
        if self._started or self._closed.is_set():
            raise CaptureError("capture already attempted; no retry")
        self._started = True
        logging.info("voice input: %s", json.dumps(self.status(), ensure_ascii=False))
        try:
            self._process = self._factory(self._argv)
            output = self._process.stdout
            if output is None:
                raise CaptureError("capture missing PCM output")
            buffer = bytearray()
            received = False
            zero_frames = 0
            last_data = time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(output, selectors.EVENT_READ)
                while not self._closed.is_set():
                    if not selector.select(0.05):
                        if time.monotonic() - last_data >= self._timeout:
                            raise CaptureError(
                                "microphone produced no audio; check app permission/device"
                            )
                        continue
                    payload = os.read(output.fileno(), FRAME_BYTES * 16)
                    if not payload:
                        if self._process.wait(timeout=1) != 0 or buffer or not received:
                            raise CaptureError("microphone capture failed or truncated; no retry")
                        return
                    received = True
                    last_data = time.monotonic()
                    buffer.extend(payload)
                    while len(buffer) >= FRAME_BYTES:
                        frame = bytes(buffer[:FRAME_BYTES])
                        del buffer[:FRAME_BYTES]
                        zero_frames = zero_frames + 1 if not any(frame) else 0
                        if zero_frames >= self._zero_limit:
                            raise ZeroAudioError(
                                "device output is all-zero; possibly wrong microphone: "
                                f"{self.selected_device.name} "
                                f"index={self.selected_device.selector}; no retry"
                            )
                        yield frame
        except (OSError, subprocess.SubprocessError) as exc:
            raise CaptureError(
                f"microphone capture failed ({type(exc).__name__}); no retry"
            ) from None
        finally:
            self.close()

    def close(self) -> None:
        self._closed.set()
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


class AfplayPlayer:
    def __init__(self, executable: str = "/usr/bin/afplay", *, command: str | None = None) -> None:
        self.executable = executable
        override = command if command is not None else os.environ.get("JARVIS_VOICE_PLAYER")
        self._command = shlex.split(override) if override is not None else [executable]
        if not self._command or any("\0" in arg for arg in self._command):
            raise ValueError("invalid voice player command")
        if sum(arg.count("{audio}") for arg in self._command) > 1:
            raise ValueError("voice player accepts at most one {audio} placeholder")
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()

    @property
    def command_executable(self) -> str:
        return self._command[0]

    @property
    def playing(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            status = self._process.poll()
            if status is not None and status != 0:
                raise LiveError("audio playback failed")
            return status is None

    def start(self, path: Path) -> None:
        with self._lock:
            self.stop()
            self._process = subprocess.Popen(
                [arg.replace("{audio}", str(path)) for arg in self._command]
                + ([] if any("{audio}" in arg for arg in self._command) else [str(path)]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


class LiveSession:
    """Single capture consumer. Push mode arms one utterance; not a wake-word detector.

    Microphone PCM is inspected during playback in both modes for acoustic barge-in.
    Push mode otherwise discards unarmed frames. This is not a physical mic mute.
    """

    def __init__(
        self, pipeline: TurnPipeline, *, mode: str = "push", config: LiveConfig | None = None
    ) -> None:
        if mode not in {"push", "always"}:
            raise ValueError("mode must be push or always")
        self.pipeline = pipeline
        self.mode = mode
        self.segmenter = EnergySegmenter(config or LiveConfig())
        self.armed = mode == "always"
        self._barge = False
        self._lock = threading.RLock()

    def toggle_push(self) -> None:
        with self._lock:
            if self.mode != "push":
                return
            if self.armed:
                utterance = self.segmenter.finish()
                self.armed = self._barge = False
                if utterance:
                    self.pipeline.submit(utterance)
            else:
                self.pipeline.interrupt()
                self.segmenter.reset()
                self.armed = True

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            playback = self.pipeline.playing
            event = self.segmenter.feed(
                pcm,
                enabled=self.armed or playback or self._barge,
                playback=playback,
            )
            if event.started:
                self._barge = True
                if playback:
                    # Stop sound at onset, not after ASR or end-of-utterance detection.
                    self.pipeline.interrupt()
                # While a reply is still being generated, an onset may be a cough or
                # a door. Only a complete utterance (submit) replaces that turn.
            if event.utterance is not None:
                self.pipeline.submit(
                    event.utterance, ended_at=time.monotonic() - event.trailing_silence_seconds
                )
            if event.ended:
                self._barge = False
                self.armed = self.mode == "always"

    def close(self) -> bool:
        return self.pipeline.close()


def run_session(
    capture: AudioCapture,
    session: LiveSession,
    stop: threading.Event | None = None,
) -> int:
    code = 0
    last_status = time.monotonic()
    try:
        for pcm in capture.frames():
            if stop is not None and stop.is_set():
                break
            session.feed(pcm)
            now = time.monotonic()
            if now - last_status >= NOISE_WINDOW_SECONDS:
                logging.info(
                    "voice threshold: %s",
                    json.dumps(session.segmenter.status(playback=session.pipeline.playing)),
                )
                last_status = now
    except KeyboardInterrupt:
        pass
    except (CaptureError, OSError, LiveError) as exc:
        logging.error(
            "voice capture stopped (%s); no retry; check microphone permission/device",
            type(exc).__name__,
        )
        if isinstance(exc, CaptureError):
            logging.error("%s", str(exc))
        code = 2
    finally:
        capture.close()
        if not session.close():
            logging.warning("voice worker is still completing a cancelled blocking operation")
            code = max(code, 3)
    return code


def local_transcribe(path: Path) -> str:
    return voice.transcribe(path)["text"]


def local_speech(text: str, directory: Path) -> Iterator[Path]:
    for chunk in voice.speak_stream(text, directory):
        yield Path(chunk.path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--mode", choices=["push", "always"], default="push")
    parser.add_argument(
        "--device", help="Unique audio name substring; default prefers built-in mic"
    )
    parser.add_argument(
        "--status", action="store_true", help="Print input selection without recording"
    )
    parser.add_argument("--player", help="Acceptance player argv; overrides JARVIS_VOICE_PLAYER")
    parser.add_argument("--zero-audio-seconds", type=float, default=5.0)
    parser.add_argument("--ffmpeg", default=os.environ.get("JARVIS_VOICE_FFMPEG"))
    parser.add_argument(
        "--energy-threshold", type=float, help="Fixed RMS override; omitted adapts to room noise"
    )
    parser.add_argument("--playback-multiplier", type=float, default=3.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if sys.platform != "darwin":
            raise LiveError("live capture currently requires macOS AVFoundation")
        executable = args.ffmpeg or shutil.which("ffmpeg")
        player = AfplayPlayer(command=args.player)
        if executable is None or shutil.which(player.command_executable) is None:
            raise LiveError("ffmpeg and selected player are required; no automatic install")
        if args.mode == "push" and not args.status and not sys.stdin.isatty():
            raise LiveError("push mode requires an interactive terminal")
        config = LiveConfig(
            energy_threshold=args.energy_threshold, playback_multiplier=args.playback_multiplier
        )
        client = TimelineClient(args.url)
        capture = FFmpegCapture(executable, args.device, zero_audio_seconds=args.zero_audio_seconds)
        if args.status:
            # --status is deliberately non-recording: null noise means unsampled,
            # not a fabricated room measurement. Live status is logged below.
            print(
                json.dumps(
                    {**capture.status(), **EnergySegmenter(config).status()}, ensure_ascii=False
                )
            )
            return 0
        pipeline = TurnPipeline(local_transcribe, client.reply, local_speech, player)
        session = LiveSession(pipeline, mode=args.mode, config=config)
        stop = threading.Event()

        def signal_stop(signum: int, frame: object) -> None:
            stop.set()

        previous = signal.signal(signal.SIGTERM, signal_stop)

        def keyboard() -> None:
            while not stop.is_set():
                if sys.stdin.readline() == "":
                    stop.set()
                    return
                session.toggle_push()

        if args.mode == "push":
            threading.Thread(target=keyboard, name="jarvis-voice-key", daemon=True).start()
        try:
            return run_session(capture, session, stop)
        finally:
            stop.set()
            signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, LiveError) as exc:
        reason = str(exc) if isinstance(exc, LiveError) else "invalid options or local dependency"
        logging.error("voice startup failed (%s): %s; no retry", type(exc).__name__, reason)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
