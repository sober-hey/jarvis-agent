# apps/watchos — Apple Watch 端

表盘上的球体入口，抬手即可说话；令牌在手表上单独导入，不跟随手机。

工程在 `JarvisWatch/`，详细说明见 [`JarvisWatch/README.md`](JarvisWatch/README.md)，
协议见 [`JarvisWatch/PROTOCOL.md`](JarvisWatch/PROTOCOL.md)。

```sh
cd apps/watchos/JarvisWatch
xcodebuild -project JarvisWatch.xcodeproj -scheme JarvisWatch \
  -destination 'generic/platform=watchOS Simulator' build
```

真机需要配对的 iPhone 与开发者签名；手表端不保存模型密钥。
