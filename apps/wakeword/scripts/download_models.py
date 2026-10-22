"""Download original openWakeWord assets via required mirror, resume safely."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models' / 'original'
MODELS.mkdir(parents=True, exist_ok=True)
NAMES = ['hey_jarvis_v0.1.onnx', 'embedding_model.onnx', 'melspectrogram.onnx']

def download(name):
    url = 'https://ghproxy.net/https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/' + name
    dest = MODELS / name
    env = {k: v for k, v in os.environ.items() if k.upper() not in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')}
    subprocess.run(['curl', '-fL', '-C', '-', '--retry', '3', '--connect-timeout', '25', '--max-time', '300', '-o', str(dest), url], env=env, check=True)
    result = {'name': name, 'source': url, 'bytes': dest.stat().st_size, 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest()}
    print(json.dumps(result), flush=True)
    return result

if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(download, NAMES))
    (MODELS / 'downloads.json').write_text(json.dumps(results, indent=2))
