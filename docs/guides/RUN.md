# 启动手册

从零到能说话，按顺序做。每一步都有验证方式，做完能自查。

## 0. 你要准备什么

| 需要 | 说明 |
|---|---|
| macOS 14+ 或 Linux（Ubuntu 22.04+） | 开发与自用推荐 macOS；Linux 用于服务器 |
| Python 3.12 | 主程序 |
| Python 3.14 | 引擎运行环境用（脚本会自己找/装） |
| [uv](https://docs.astral.sh/uv/) | 装依赖、建引擎环境 |
| 一个 OpenAI 兼容的模型网关 + API Key | 大脑 |
| （可选）火山引擎语音合成账号 | TTS；不配就用 `--tts bailian` 或纯文本 |
| （可选）百炼账号 | ASR 与 CosyVoice |
| ffmpeg | 音频转换（语音链路必需） |

## 1. 拉代码、装依赖

```sh
git clone https://github.com/sober-hey/jarvis-agent.git jarvis
cd jarvis
uv sync --frozen          # 生成 .venv（Python 3.12）
```

验证：

```sh
.venv/bin/python3 -c "import pydantic, typer; print('ok')"
```

## 2. 建引擎运行环境（关键一步）

模型调用与内心循环跑在一个独立的 Python 3.14 venv 里，它不在仓库里，要自己建。
需要先把上游 agent engine（Hermes Agent，MIT）放到 `components/agent_engine/upstream/`
（要有 `pyproject.toml` 与 `uv.lock`）。

```sh
scripts/build_engine_runtime.sh
```

脚本会：找 Python 3.14 → 建 venv → 按锁文件装 66 个包 → 补装产品侧依赖 →
`uv pip check` → 导入冒烟 → 写 `packaging/runtime/BUILD_RECORD.json`。

验证：

```sh
scripts/build_engine_runtime.sh --check
# 期望：环境可用：…/engine-venv（Python 3.14，77 个包）
```

## 3. 写配置

所有密钥都不进代码，放在 `~/.jarvis/`：

```sh
mkdir -p ~/.jarvis
cat > ~/.jarvis/config.json <<'JSON'
{
  "model": "你的模型名",
  "provider": "custom",
  "base_url": "https://你的网关/v1",
  "api_mode": "chat",
  "context_length": 128000,
  "reasoning_effort": "medium"
}
JSON
printf '%s' '你的 API Key' > ~/.jarvis/api.key
chmod 600 ~/.jarvis/api.key
```

语音网关另需一个设备共享令牌（44 字符随机串）：

```sh
LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 43 > ~/.jarvis/voice-gateway.token
chmod 600 ~/.jarvis/voice-gateway.token
```

## 4. 先跑测试，确认环境没问题

```sh
mkdir -p ~/.tmp-pytest

# 内核层与契约（约 40 秒，应 2381 passed）
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=src:packaging:. .venv/bin/python3 -m pytest tests -q

# 语音子系统（约 15 秒；引擎环境缺一条会 skip）
PYTHONPATH=voice:packaging .venv/bin/python3 -m pytest voice/tests -q

# 运行时全量（约 20 分钟，别并行跑两份）
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=packaging .venv/bin/python3 -m pytest packaging/ -q
```

## 5. 起服务

### 前台手动起（调试用）

```sh
# 语音网关：设备接入，默认 8765
PYTHONPATH=voice:packaging .venv/bin/python3 voice/voice_gateway.py \
  --token-file ~/.jarvis/voice-gateway.token \
  --config ~/.jarvis/config.json \
  --brain-key ~/.jarvis/api.key \
  --data-dir ~/.jarvis/loop-data \
  --log ~/.jarvis/voice-gateway.log

# 另一个终端：守护进程（内心循环）
PYTHONPATH=packaging .venv/bin/python3 packaging/jarvis_daemon.py \
  --data-root ~/.jarvis/loop-data \
  --config ~/.jarvis/config.json \
  --key-file ~/.jarvis/api.key
```

验证网关：

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/     # 期望 401（无令牌被拒）
lsof -nP -iTCP:8765 -sTCP:LISTEN                                    # 期望只监听回环/内网地址
```

### 开机自启（macOS）

```sh
# 网关：com.jarvis.voice-gateway
cp deploy/launchd/com.jarvis.voice-gateway.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.voice-gateway.plist
launchctl kickstart -k gui/$(id -u)/com.jarvis.voice-gateway

# 守护进程：com.jarvis.daemon
cp deploy/launchd/com.jarvis.daemon.plist ~/Library/LaunchAgents/
# 先把 plist 里的路径改成你的仓库位置
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.daemon.plist
```

> `deploy/launchd/` 里的 plist 是模板，路径写成 `{{REPO}}` / `{{HOME}}` 占位，用之前 sed 替换成实际路径
> （替换命令见 [../../deploy/launchd/README.md](../../deploy/launchd/README.md)）。

### 服务器部署（Linux）

见 [../deploy/README.md](../../deploy/README.md)：systemd 单元、目录约定、密钥用 `LoadCredential` 注入。

## 6. 接设备

| 端 | 做法 |
|---|---|
| Mac | `cd apps/mac/JarvisMac && python3 Scripts/build_app.py` → `Output/JARVIS.app`，设置页填网关地址与令牌 |
| iPhone | Xcode 打开 `apps/ios/JarvisVoice`，选自己的 Team，Run |
| Watch | Xcode 打开 `apps/watchos/JarvisWatch`，同上 |
| Web 面板 | 随守护进程起，只监听本机 |

网关地址填 `ws://<内网地址>:8765`；跨网用 Tailscale 时填 `wss://<tailscale 地址>:8765`。

## 7. 出问题先看哪里

| 现象 | 先查 |
|---|---|
| 设备连不上 | 网关日志 `~/.jarvis/voice-gateway.log`；令牌是否一致；地址是否走 IPv6 |
| 说话没反应 | 声纹是否把你自己判成"他人"（日志 `speaker_foreign`）；端点检测阈值 |
| 只有一句就断 | `playback_done` 有没有回；见 [VOICE.md](VOICE.md) |
| 模型一直失败 | `~/.jarvis/api.key` 与 `config.json`；`scripts/route_compliance_probe.py` |
| 守护进程起不来 | `~/.jarvis/daemon.log`；数据根权限是否 0700 |
