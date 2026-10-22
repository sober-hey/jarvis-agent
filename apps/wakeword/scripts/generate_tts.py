#!/usr/bin/env python3
"""Generate the reproducible DashScope wakeword corpus.

Reads only ~/.jarvis/dashscope.key. It writes original provider audio, normalized
16 kHz mono PCM WAV, per-item metadata, and a deterministic manifest. No model
training or evaluation is performed.
"""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ORIGINAL = DATA / "original"
WAV = DATA / "wav"
META = DATA / "metadata"
KEY_PATH = Path.home() / ".jarvis" / "dashscope.key"
QWEN_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
COSY_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
CLONE = "cosyvoice-v3.5-flash-jarvis-f7bec1631ddf4725841995ba9c711376"
QWEN_VOICES = ["Cherry", "Serena", "Ethan", "Chelsie"]
FFMPEG = shutil.which("ffmpeg") or "/Users/sober/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg"
FFPROBE = shutil.which("ffprobe") or str(Path(FFMPEG).with_name("ffprobe"))

POSITIVE = [
    "Jarvis, open the study timer.", "Jarvis, what is on my schedule today?",
    "Jarvis, start a focus session.", "Jarvis, read my latest messages.",
    "Jarvis, turn on the desk light.", "Jarvis, lower the bedroom temperature.",
    "Jarvis, remind me to call Mom at six.", "Jarvis, play my evening playlist.",
    "Jarvis, how long until sunset?", "Jarvis, add milk to my shopping list.",
    "Jarvis, pause the music.", "Jarvis, set an alarm for seven thirty.",
    "Jarvis, check the front door camera.", "Jarvis, start the coffee maker.",
    "Jarvis, give me a short weather update.", "Jarvis, mute the living room speaker.",
    "Jarvis, find my notes about the meeting.", "Jarvis, send a message to Alex.",
    "Jarvis, what is the battery level?", "Jarvis, open the calendar.",
    "Jarvis, begin recording this note.", "Jarvis, turn off the hallway light.",
    "Jarvis, set the room brightness to forty percent.", "Jarvis, stop the timer.",
]
CONTROL = [
    "Hey Jarvis, open the study timer.", "Hey Jarvis, what is on my schedule?",
    "Hey Jarvis, start a focus session.", "Hey Jarvis, read my latest messages.",
    "Hey Jarvis, turn on the desk light.", "Hey Jarvis, play my evening playlist.",
    "Hey Jarvis, set an alarm for seven.", "Hey Jarvis, check the front door camera.",
]
NEGATIVE_ZH = [
    "今天早上请打开学习计时器。", "我想知道今天的日程安排。",
    "请开始一段专注时间。", "帮我读一下最新的消息。",
    "把书桌上的灯打开。", "卧室温度调低两度。",
    "六点提醒我给妈妈打电话。", "播放我的晚间歌单。",
    "现在距离日落还有多久？", "在购物清单里加上牛奶。",
    "请把窗帘打开一半。", "下午三点提醒我喝水。",
    "帮我查一下明天的空气质量。", "把手机调成静音模式。",
    "厨房的灯亮度调到百分之二十。", "播放一段舒缓的音乐。",
    "我需要一个十五分钟的倒计时。", "把今天的会议记到日历里。",
    "请告诉我现在的时间。", "打开客厅的空气净化器。",
    "晚饭后提醒我取快递。", "把这句话记进我的备忘录。",
    "请关闭书房的风扇。", "帮我找一条去车站的路线。",
    "检查一下明天有没有下雨。",    "把电视音量调小一点。",
]
NEGATIVE_ZH_EXTRA = [
    "请把阳台的门关好。", "晚上八点提醒我收衣服。",
    "帮我查一下这本书的作者。", "把早餐的闹钟设为六点半。",
    "打开卧室的加湿器。", "请把今天的步数记下来。",
]
NEGATIVE_EN = [
    "Please open the study timer for me.", "What is on my schedule today?",
    "Start a quiet focus session now.", "Could you read my latest messages?",
    "Turn on the desk lamp, please.", "Lower the bedroom temperature a little.",
    "Remind me to call my mother at six.", "Play the playlist from this evening.",
    "How much time remains before sunset?", "Add milk to the shopping list.",
    "Pause the music in the living room.", "Set an alarm for seven thirty.",
    "Check the camera by the front door.", "Please start the coffee maker.",
    "Give me a brief update about the weather.", "Mute the speaker in the living room.",
]
NEGATIVE_NEAR = [
    "Service the car before the weekend.", "The harvest report is ready.",
    "Travis left a note on the table.", "Jarred sauce is in the pantry.",
    "The jar is beside the vase.", "Harvis was the name in that old story.",
    "Please archive the meeting transcript.", "The garden needs water this afternoon.",
    "I heard a soft chime from the hallway.", "The travel plan changed this morning.",
    "A silver train passes the station at noon.", "The server is ready for maintenance.",
    "Carla is waiting near the library.", "The forecast says rain after dinner.",
    "We should review the invoice tomorrow.", "The market opens at nine o'clock.",
]

def samples():
    out = []
    groups = [("positive", POSITIVE, "English"), ("control", CONTROL, "English"),
              ("negative_zh", NEGATIVE_ZH, "Chinese"), ("negative_en", NEGATIVE_EN, "English"),
              ("negative_near", NEGATIVE_NEAR, "English"), ("negative_zh", NEGATIVE_ZH_EXTRA, "Chinese")]
    n = 0
    for category, texts, lang in groups:
        for text in texts:
            n += 1
            sid = f"{category}_{n:03d}"
            # Hash assignment is stable if the source list order is preserved.
            split = "holdout" if int(hashlib.sha256(sid.encode()).hexdigest()[:8], 16) % 5 == 0 else "calibration"
            rate = (0.94, 1.0, 1.06)[int(hashlib.sha256((sid+text).encode()).hexdigest()[:2], 16) % 3]
            voice = (QWEN_VOICES + [CLONE])[n % 5]
            out.append({"source_id": sid, "category": category, "text": text,
                        "language_type": lang, "split": split, "voice": voice,
                        "voice_kind": "clone" if voice == CLONE else "system",
                        "tempo": rate})
    return out

def clean_error(value):
    if isinstance(value, dict):
        return {k: clean_error(v) for k, v in value.items() if k.lower() not in {"url", "data", "audio_data"}}
    if isinstance(value, list): return [clean_error(v) for v in value]
    if isinstance(value, str):
        return value[:500]
    return value

def post(item, key, timeout):
    if item["voice_kind"] == "clone":
        model = "cosyvoice-v3.5-flash"
        body = {"model": model, "input": {"text": item["text"], "voice": item["voice"],
                "format": "mp3", "sample_rate": 24000}}
        ext = ".mp3"
    else:
        model = "qwen3-tts-flash"
        body = {"model": model, "input": {"text": item["text"], "voice": item["voice"],
                "language_type": item["language_type"]}}
        ext = ".wav"
    endpoint = COSY_ENDPOINT if item["voice_kind"] == "clone" else QWEN_ENDPOINT
    req = Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                  headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    started = time.time()
    try:
        with urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
        audio = result.get("output", {}).get("audio", {})
        url = audio.get("url")
        if not url: raise RuntimeError(result.get("message") or "response has no audio URL")
        dreq = Request(url, headers={"User-Agent": "jarvis-wakeword-corpus/1"})
        with urlopen(dreq, timeout=timeout) as resp:
            raw = resp.read()
        original = ORIGINAL / (item["source_id"] + ext)
        original.write_bytes(raw)
        return item, model, original, result.get("usage", {}), time.time() - started, None
    except Exception as exc:
        return item, model, None, {}, time.time() - started, f"{type(exc).__name__}: {str(exc)[:400]}"

def normalize(original, out, tempo):
    af = f"atempo={tempo:.2f}" if tempo != 1.0 else "anull"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(original),
           "-af", af, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True, timeout=90)

def duration(path):
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
    try: return round(float(subprocess.check_output(cmd, text=True).strip()), 4)
    except Exception: return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated source IDs to generate; merges into existing manifest")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not KEY_PATH.exists() and not args.dry_run:
        raise SystemExit(f"missing credential file: {KEY_PATH}")
    for d in (ORIGINAL, WAV, META): d.mkdir(parents=True, exist_ok=True)
    items = samples()
    if args.only:
        wanted = set(args.only.split(","))
        items = [item for item in items if item["source_id"] in wanted]
    if args.limit: items = items[:args.limit]
    key = KEY_PATH.read_text().strip() if not args.dry_run else ""
    results = []
    if args.dry_run:
        for item in items:
            results.append({"source_id": item["source_id"], "status": "planned", **item})
    else:
        # HTTP_PROXY/HTTPS_PROXY are deliberately removed for DashScope direct access.
        env = os.environ
        env.pop("HTTP_PROXY", None); env.pop("HTTPS_PROXY", None)
        env.pop("http_proxy", None); env.pop("https_proxy", None)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(post, item, key, args.timeout) for item in items]
            for fut in concurrent.futures.as_completed(futures):
                item, model, original, usage, elapsed, error = fut.result()
                rec = {"source_id": item["source_id"], "text": item["text"],
                       "category": item["category"], "language_type": item["language_type"],
                       "split": item["split"], "voice": item["voice"],
                       "voice_kind": item["voice_kind"], "tempo": item["tempo"],
                       "model": model, "usage": clean_error(usage), "elapsed_seconds": round(elapsed, 3),
                       "status": "error" if error else "generated", "error": error}
                if original:
                    wav = WAV / (item["source_id"] + ".wav")
                    try:
                        normalize(original, wav, item["tempo"])
                        rec.update({"original_audio": str(original.relative_to(DATA)),
                                    "audio": str(wav.relative_to(DATA)), "duration_seconds": duration(wav),
                                    "sample_rate_hz": 16000, "channels": 1, "encoding": "PCM_S16LE",
                                    "sha256_original": hashlib.sha256(original.read_bytes()).hexdigest(),
                                    "sha256_audio": hashlib.sha256(wav.read_bytes()).hexdigest()})
                    except Exception as exc:
                        rec["status"] = "error"
                        rec["error"] = f"normalization: {type(exc).__name__}: {str(exc)[:400]}"
                results.append(rec)
                (META / (item["source_id"] + ".json")).write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
                print(rec["source_id"], rec["status"], flush=True)
    if args.dry_run:
        print(json.dumps({"requested": len(items), "generated": 0, "errors": 0,
                          "planned": len(items)}, ensure_ascii=False))
        return
    results.sort(key=lambda r: r["source_id"])
    if args.only and (DATA / "manifest.json").exists():
        prior = json.loads((DATA / "manifest.json").read_text())
        by_id = {row["source_id"]: row for row in prior.get("items", [])}
        by_id.update({row["source_id"]: row for row in results})
        results = [by_id[key] for key in sorted(by_id)]
    generated = sum(r.get("status") == "generated" for r in results)
    errors = sum(r.get("status") == "error" for r in results)
    manifest = {"schema": "jarvis-wakeword-tts/v1",
                "source_seed": "embedded ordered source lists; sha256 source_id assignment",
                "endpoints": {"qwen3-tts-flash": QWEN_ENDPOINT, "cosyvoice-v3.5-flash": COSY_ENDPOINT},
                "normalization": {"sample_rate_hz": 16000, "channels": 1, "encoding": "PCM_S16LE"},
                "voices": {"qwen3-tts-flash": QWEN_VOICES, "cosyvoice-v3.5-flash": [CLONE]},
                "counts": {"requested": len(results), "generated": generated,
                           "errors": errors, "planned": sum(r.get("status") == "planned" for r in results)}, "items": results}
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest["counts"], ensure_ascii=False))

if __name__ == "__main__":
    main()
