# macOS launchd 模板

两个 plist 是**模板**，`{{REPO}}` 是仓库路径、`{{HOME}}` 是用户目录，用之前先替换：

```sh
REPO=$(pwd)          # 在仓库根执行
HOME_DIR=$HOME
for f in deploy/launchd/*.plist; do
  sed "s|{{REPO}}|$REPO|g; s|{{HOME}}|$HOME_DIR|g" "$f" > "$HOME/Library/LaunchAgents/$(basename "$f")"
done

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.voice-gateway.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.daemon.plist
```

| 文件 | 起什么 | 依赖 |
|---|---|---|
| `com.jarvis.voice-gateway.plist` | 语音网关（设备接入，8765） | 仓库 `.venv`（Python 3.12）、令牌文件、配置 |
| `com.jarvis.daemon.plist` | 守护进程（内心循环） | **引擎运行环境**（`scripts/build_engine_runtime.sh` 建的 Python 3.14 venv） |

## 说明

- 两个进程互相独立：网关挂了不影响守护进程思考，反之亦然。
- 守护进程必须用引擎运行环境里的 python，不是仓库 `.venv`——两者 Python 版本不同（3.14 vs 3.12）。
- iMessage 入站/出站是可选的：需要时在 daemon plist 里加 `JARVIS_IMESSAGE_ACCOUNT` 与
  `JARVIS_IMESSAGE_SIR` 两个环境变量（见文件头注释）。不加则该通道不可用，其它功能正常。
- 日志：`~/.jarvis/daemon.log`、`~/.jarvis/voice-gateway.log`。
- 卸载：`launchctl bootout gui/$(id -u)/com.jarvis.daemon`（另一个同理）。
