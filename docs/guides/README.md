# 操作手册

遇到具体问题再翻，不用通读。

| 文档 | 什么时候看 |
|---|---|
| [RUN.md](RUN.md) | **第一次部署**，从零到能说话：依赖 → 引擎环境 → 配置 → 测试 → 起服务 → 接设备 → 排错 |
| [VOICE.md](VOICE.md) | 语音出问题（没反应 / 只有一句 / 声音小 / 别人说话它也答）：先取证的三条命令、故障对照表、参数出处 |
| [REVIEW.md](REVIEW.md) | 要发起一次评审、或想知道评审怎么取证 |

## 三条最常用的取证命令

```sh
tail -50 ~/.jarvis/voice-gateway.log          # 网关日志
tail -50 ~/.jarvis/daemon.log                 # 守护进程日志
launchctl list | grep jarvis                  # 两个服务是否在跑
```

生产环境（Linux）看 `journalctl -u jarvis-voice-gateway -u jarvis-daemon -n 100`。
