#!/bin/sh
# 一键构建引擎运行环境：packaging/runtime/engine-venv
#
# 为什么需要它：语音和内心循环之外，模型调用、上游 agent engine 都跑在这个独立 venv 里。
# 没有它，packaging/ 里大量用例会跳过或失败，产品也起不来。以前这份环境是手工建的，
# 换机器只能照 BUILD_MANIFEST.json 抄命令；这个脚本把那串命令固化下来。
#
# 前置条件：
#   1. uv（https://docs.astral.sh/uv/）——脚本会自己找，也可用 UV_BIN 指定
#   2. Python 3.14——uv 能自动装；要禁用下载就 --no-python-download
#   3. 上游 agent engine 已放到 components/agent_engine/upstream（含 pyproject.toml 与 uv.lock）
#
# 用法：
#   scripts/build_engine_runtime.sh                 # 构建到默认路径
#   scripts/build_engine_runtime.sh --force         # 已存在也重建
#   scripts/build_engine_runtime.sh --dest /tmp/x   # 构建到别处（测试用）
#   scripts/build_engine_runtime.sh --check         # 只检查现有环境，不构建
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
DEST="$REPO/packaging/runtime/engine-venv"
UPSTREAM="$REPO/components/agent_engine/upstream"
FORCE=0
MODE=build
ALLOW_PY_DOWNLOAD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --dest) DEST="$2"; shift ;;
    --check) MODE=check ;;
    --no-python-download) ALLOW_PY_DOWNLOAD=0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

# ---------- 检查现有环境 ----------
check_runtime() {
  PY="$1/bin/python3"
  [ -x "$PY" ] || die "$1 不是可用的 venv（缺 bin/python3）"
  VER=$("$PY" -I -B -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  [ "$VER" = "3.14" ] || die "引擎运行环境要 Python 3.14，当前是 $VER"
  "$PY" -I -B -c 'import openai,rich,pydantic,prompt_toolkit,dotenv,httpx,packaging,fastapi,uvicorn,cryptography,psutil' \
    || die "上游核心依赖导入失败"
  # jsonschema 等在主 venv 里，引擎 venv 不需要；这里只查引擎真正会 import 的。
  "$PY" -I -B -c 'import numpy,pandas,sherpa_onnx,websockets,ddgs' \
    || die "产品侧依赖导入失败（numpy/pandas/sherpa_onnx/websockets/ddgs）"
  N=$("$PY" -I -B -c 'import sysconfig; from importlib import metadata; print(len(list(metadata.distributions(path=[sysconfig.get_path("purelib")]))))')
  say "环境可用：$1（Python ${VER}，${N} 个包）"
}

if [ "$MODE" = check ]; then
  check_runtime "$DEST"
  exit 0
fi

# ---------- 前置条件 ----------
[ -f "$UPSTREAM/pyproject.toml" ] || die "缺少上游引擎：$UPSTREAM
这是第三方代码（MIT），不随本仓库分发。拿到上游后放到该目录，再重跑本脚本。"
[ -f "$UPSTREAM/uv.lock" ] || die "缺少 $UPSTREAM/uv.lock（没有锁文件无法可复现地安装）"

UV=${UV_BIN:-}
if [ -z "$UV" ]; then
  UV=$(command -v uv 2>/dev/null || true)
fi
[ -n "$UV" ] && [ -x "$UV" ] || die "找不到 uv。装好 uv 或用 UV_BIN=/path/to/uv 指定。"

if [ -e "$DEST" ] && [ "$FORCE" -eq 0 ]; then
  say "$DEST 已存在，只做检查（要重建加 --force）"
  check_runtime "$DEST"
  exit 0
fi

# ---------- 隔离的构建环境 ----------
# 用独立的 HOME/TMPDIR，避免污染真实 home，也避免宿主 Hermes 的配置混进来。
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/jarvis-engine-build.XXXXXX")
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/home" "$SCRATCH/tmp" "$SCRATCH/cache"

say "1/5 找 Python 3.14"
# 在临时目录里找：仓库 pyproject 要求 3.12，在仓库内跑 uv python find 会被项目约束干扰。
find_py() { ( cd "$SCRATCH" && "$UV" python find 3.14 2>/dev/null || true ); }
BASE=$(find_py)
if [ -z "$BASE" ] && [ "$ALLOW_PY_DOWNLOAD" -eq 1 ]; then
  say "   本机没有，交给 uv 下载 3.14"
  ( cd "$SCRATCH" && "$UV" python install 3.14 >/dev/null 2>&1 ) || true
  BASE=$(find_py)
fi
[ -n "$BASE" ] && [ -x "$BASE" ] || die "没有可用的 Python 3.14（可用 uv python install 3.14 装，或去掉 --no-python-download）"
say "   $BASE"

say "2/5 创建 venv（--without-pip --copies）"
[ -e "$DEST" ] && rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
"$BASE" -I -B -m venv --without-pip --copies "$DEST"

say "3/5 按锁文件安装上游依赖（可能需要几分钟、需要网络）"
env -i \
  HOME="$SCRATCH/home" \
  PATH="/usr/bin:/bin" \
  TMPDIR="$SCRATCH/tmp" \
  XDG_CACHE_HOME="$SCRATCH/cache" \
  XDG_CONFIG_HOME="$SCRATCH/cache/config" \
  UV_CACHE_DIR="$SCRATCH/cache/uv" \
  UV_PROJECT_ENVIRONMENT="$DEST" \
  UV_PYTHON_DOWNLOADS=never \
  UV_HTTP_TIMEOUT=40 \
  UV_HTTP_RETRIES=1 \
  UV_CONCURRENT_DOWNLOADS=4 \
  UV_CONCURRENT_BUILDS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONNOUSERSITE=1 \
  LANG=en_US.UTF-8 \
  "$UV" --directory "$UPSTREAM" sync \
    --locked --no-install-project --no-default-groups --no-dev --no-build \
    --no-python-downloads --python "$BASE" --link-mode copy --color never

say "3b/5 补装产品侧依赖（上游锁文件里没有，但产品代码要 import）"
env -i HOME="$SCRATCH/home" PATH="/usr/bin:/bin" TMPDIR="$SCRATCH/tmp" \
  UV_CACHE_DIR="$SCRATCH/cache/uv" \
  "$UV" pip install --python "$DEST/bin/python3" --color never \
    "numpy>=2,<3" "pandas>=2.2,<3" "sherpa-onnx>=1.13,<2" \
    "websockets>=15,<17" "httpx>=0.27,<1" "ddgs>=9,<10"

say "4/5 依赖一致性检查"
env -i HOME="$SCRATCH/home" PATH="/usr/bin:/bin" TMPDIR="$SCRATCH/tmp" \
  UV_CACHE_DIR="$SCRATCH/cache/uv" \
  "$UV" pip check --python "$DEST/bin/python3" || die "依赖不一致（uv pip check 失败）"

say "5/5 导入冒烟 + 记录"
check_runtime "$DEST"

RECORD="$REPO/packaging/runtime/BUILD_RECORD.json"
python3 - "$DEST" "$RECORD" "$BASE" <<'PY'
import json, subprocess, sys, time

dest, record, base = sys.argv[1], sys.argv[2], sys.argv[3]
probe = (
    "import sys, sysconfig, json, platform\n"
    "from importlib import metadata\n"
    "purelib = sysconfig.get_path('purelib')\n"
    "names = sorted(d.metadata['Name'] for d in metadata.distributions(path=[purelib]))\n"
    "print(json.dumps({'version': sys.version, 'prefix': sys.prefix,\n"
    "                  'base_prefix': sys.base_prefix, 'machine': platform.machine(),\n"
    "                  'packages': names}))\n"
)
out = subprocess.run([dest + "/bin/python3", "-I", "-B", "-c", probe],
                     capture_output=True, text=True)
info = json.loads(out.stdout) if out.returncode == 0 else {"error": out.stderr[-500:]}
record_data = {
    "schema_version": 1,
    "built_by": "scripts/build_engine_runtime.sh",
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "python_base": base,
    "runtime": info,
}
with open(record, "w", encoding="utf-8") as fh:
    json.dump(record_data, fh, indent=2, ensure_ascii=False)
print(f"构建记录已写入 {record}")
print(f"包数量 {len(info.get('packages', []))}")
PY

say "完成：$DEST"
say "验证：scripts/build_engine_runtime.sh --check"
