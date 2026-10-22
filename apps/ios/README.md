# apps/ios — iPhone 端

球体界面的语音入口：点一下球体开始说话，说完自动断句上传；回复在同一个音频引擎里播放，
避免回声消除把机器人自己的声音当外部音频压低。

工程在 `JarvisVoice/`，详细说明见 [`JarvisVoice/README.md`](JarvisVoice/README.md)，
协议见 [`JarvisVoice/PROTOCOL.md`](JarvisVoice/PROTOCOL.md)。

```sh
cd apps/ios/JarvisVoice
xcodebuild -scheme JarvisVoice -destination 'generic/platform=iOS Simulator' build
```

装机要你自己的 Apple 开发者签名：Xcode 打开工程 → 选 Team → 选设备 → Run。
