# 参与开发

## 环境

- macOS 14+，Python 3.12（仓库内 `.venv`），Xcode 16+（客户端）
- 依赖：`uv sync --frozen`；不改系统 Python
- 运行期目录：`~/.jarvis/`（配置、令牌、记忆库、语音片段）

## 动手前

1. 读 [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md)：分层、入口、加功能放哪。
2. 读 [`ARCHITECTURE.md`](ARCHITECTURE.md) 的模块边界与数据流。
3. 改数据结构前读 `contracts/`（当前 DRAFT，不是冻结契约）与 `docs/design/`（设计稿，不是规格）。
4. 改设计前先看 `docs/adr/`，那里有已经做过的取舍和理由。

## 测试

```sh
# 内核层与契约
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=src:packaging:. .venv/bin/python3 -m pytest tests -q

# 运行时（全量约 20 分钟；别并行跑两份，会互相拖慢）
TMPDIR=$HOME/.tmp-pytest PYTHONPATH=packaging .venv/bin/python3 -m pytest packaging/ -q

# 语音子系统
PYTHONPATH=voice:packaging .venv/bin/python3 -m pytest voice/tests -q

# 一句话切分的仿真（Mac 客户端逻辑，不需要设备）
cd apps/mac/JarvisMac && swiftc -O Sources/JarvisIntegration/UtteranceGate.swift \
  Tests/UtteranceSimulation/main.swift -o /tmp/gate_sim && /tmp/gate_sim

# 手势/唤醒阈值回归
cd apps/mac/JarvisMac/Vendor/Activation && swift run activation-regression

# 客户端编译
cd apps/mac/JarvisMac && python3 Scripts/build_app.py
cd apps/ios/JarvisVoice && xcodebuild -scheme JarvisVoice -destination 'generic/platform=iOS Simulator' build
cd apps/watchos/JarvisWatch && xcodebuild -project JarvisWatch.xcodeproj -scheme JarvisWatch \
  -destination 'generic/platform=watchOS Simulator' build
```

## 提交约定

- 一个提交一件事，标题写"改了什么"，正文写"为什么"和"怎么验证的"。
- **不许在提交信息、代码、日志里写密钥、令牌、手机号、账号**。
- 声称"已修复"必须附验证方式（命令与输出，或日志行号）。
- 改了测试语义要申报：在提交信息里写清楚原断言、新断言、为什么。

## 目录约定

| 要加的东西 | 放哪 |
|---|---|
| 新的语音供应商 | `voice/voice_<vendor>.py`，实现 `voice_stream_tts.StreamingTTS` 或 `voice.VoiceBackend` |
| 新的设备客户端 | `apps/<platform>/` |
| 新的后台定时任务 | `packaging/daemon_wiring.py`，必须幂等 |
| 新运维脚本 | `scripts/`，要有 `--help` 与干跑开关 |
| 架构决策 | `docs/adr/ADR-XXXX_*.md` |
| 临时脚本、一次性取证 | 不要提交；留在本地 `evidence/`（已在 .gitignore） |

## 禁止

- 把模型密钥、iMessage 账号、手机号写进代码或用例（要用环境变量）。
- 在 `packaging/` 里留一次性脚本、调试打印、临时数据。
- 为让测试通过而放宽断言——真失败就修代码；确实要改语义，写进 ADR。
- 把 macOS 本地通过当成 Linux 生产通过。
- 提交第三方商业 SDK 的二进制（例如 3D 相关厂商包）。
