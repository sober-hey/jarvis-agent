#!/usr/bin/env python3
"""Deterministic generated signal fixtures, NOT recordings of people/music."""
import argparse
import array
import json
import math
from pathlib import Path
import random
import sys
import wave
from typing import Any, Dict, Iterable, List, Optional

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
out = args.output
out.mkdir(parents=True, exist_ok=True)
rate = 48000
rng = random.Random(6117)
clips: List[Dict[str, Any]] = []
fallback: List[Dict[str, Any]] = []

def save(name: str, samples: Iterable[float], label: str, kind: Optional[str] = None,
         trigger: Optional[float] = None, target: Optional[List[Dict[str, Any]]] = None) -> None:
    if target is None:
        target = clips
    data = array.array("h", (round(max(-1, min(1, x)) * 32767) for x in samples))
    if sys.byteorder != "little":
        data.byteswap()
    with wave.open(str(out / name), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(data.tobytes())
    target.append(dict(path=name, label=label, expectedKind=kind, triggerTime=trigger))

def burst(signal: List[float], start: float, frequency: float, amp: float, decay: float = 180) -> None:
    begin = round(start * rate)
    for i in range(384):
        signal[begin + i] += amp * math.sin(2 * math.pi * frequency * i / rate) * math.exp(-i / decay)

for i, (interval, amp, frequency) in enumerate([(0.22, 0.8, 2200), (0.35, 0.9, 3000), (0.48, 1, 4300)]):
    samples = [0.0] * rate
    burst(samples, 0.1, frequency, amp)
    burst(samples, 0.1 + interval, frequency, amp)
    save(f"double-clap-{i+1}.wav", samples, "generated decaying band impulse pair", "doubleClap", 0.1 + interval)
for i, (amp, frequency) in enumerate([(0.8, 8500), (0.9, 10000), (1, 11500)]):
    samples = [0.0] * rate
    burst(samples, 0.1, frequency, amp)
    save(f"snap-{i+1}.wav", samples, "generated decaying high-band impulse", "fingerSnap", 0.1)

for i in range(100):
    count = rate
    if i < 25:
        # Harmonic voiced proxy. Not intelligible speech and not claimed as such.
        f0 = 90 + i * 7
        samples = [0.2 * (0.5 + 0.5 * math.sin(2 * math.pi * 3 * j / rate)) *
                   sum(math.sin(2 * math.pi * f0 * h * j / rate) / (h*h) for h in range(1, 8)) for j in range(count)]
        label = "generated harmonic voiced proxy"
    elif i < 50:
        freqs = [220 * (2 ** (((i + step) % 24) / 12)) for step in (0, 4, 7)]
        samples = [0.1 * sum(math.sin(2 * math.pi * f * j / rate) for f in freqs) for j in range(count)]
        label = "generated triad, not recorded music"
    elif i < 70:
        samples = [rng.uniform(-0.2, 0.2) for _ in range(count)]
        label = "generated stationary white noise"
    elif i < 80:
        samples = [0.0] * count
        burst(samples, 0.15, 3000, 0.9)
        label = "single clap-like impulse (not a pair)"
    elif i < 90:
        samples = [0.0] * count
        burst(samples, 0.1, 3000, 0.9)
        burst(samples, 0.72, 3000, 0.9)
        label = "out-of-time clap-like pair"
    else:
        samples = [0.0] * count
        burst(samples, 0.1, 10000, 0.02)
        label = "subthreshold snap-like impulse"
    save(f"negative-{i:03}.wav", samples, label)

for i in range(3):
    samples = [0.1 * math.sin(2 * math.pi * (200 + i * 100) * j / rate) for j in range(rate * 4)]
    save(f"fallback-voice-{i+1}.wav", samples, "generated sustained voice-activity proxy, NOT Jarvis",
         "voiceActivityFallback", 3, fallback)

(out / "manifest.json").write_text(json.dumps(dict(synthetic=True, clips=clips), indent=2))
(out / "fallback-manifest.json").write_text(json.dumps(dict(synthetic=True, clips=fallback), indent=2))
print(f"Wrote {len(clips)} gesture/control WAVs + {len(fallback)} fallback WAVs; synthetic only")
