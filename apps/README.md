# apps/ — 设备端

三个语音入口加一个本机面板。**它们只负责采集、播放、显示状态**，判断与记忆都在服务端
（`voice/` + `packaging/`），客户端不持有模型密钥。

| 端 | 目录 | 形态 | 编译 |
|---|---|---|---|
| macOS | `mac/JarvisMac` | 菜单栏 App（SwiftUI + AVAudioEngine），带离线唤醒词与响指/拍手手势 | `python3 Scripts/build_app.py` → `Output/JARVIS.app` |
| iPhone | `ios/JarvisVoice` | 球体界面，点一下说话；已移除唤醒词 | `xcodebuild -scheme JarvisVoice -destination 'generic/platform=iOS Simulator' build` |
| Apple Watch | `watchos/JarvisWatch` | 球体 + 录音触发 | `xcodebuild -project JarvisWatch.xcodeproj -scheme JarvisWatch -destination 'generic/platform=watchOS Simulator' build` |
| 本机面板 | `web` | 纯静态页，由 `packaging/web_server.py` 提供，仅监听回环 | 无需编译 |

细节各自目录里都有 README：

- `mac/JarvisMac/README.md`：模块划分、构建、签名
- `mac/Modules/*/README.md`：四个独立 Swift 包（唤醒手势、音频可访问性、录音、WebSocket）
- `ios/JarvisVoice/README.md`、`watchos/JarvisWatch/README.md`：协议与装机

## 与服务端的契约

客户端连 `ws://<主机>:8765`，用 Bearer 令牌鉴权。消息类型、一轮对话的状态迁移见
`docs/voice/voice_gateway.md`；令牌轮换见 `scripts/rotate_gateway_token.sh`。

## 真机装机

需要你自己的 Apple 开发者签名：Xcode 打开工程 → 选 Team → 选设备 → Run。
仓库里没有、也不会有别人的证书与描述文件。
