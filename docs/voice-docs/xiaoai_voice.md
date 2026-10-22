# 小爱音箱作为 JARVIS 语音出口

## 状态与边界

实现：`packaging/voice_xiaoai.py`，复用本机已经打通的单设备小米云通道；不新增密码或复制 token。`speak(text)` 只负责 TTS 下发，`is_available()` 只查询目标设备在线状态。**API 返回成功不等于 Sir 听到了。**

本次真实在线查询返回 `credentials_invalid`；安全播报调用返回 `presence_blocked`，没有下发 TTS，尚未取得听到确认。证据：`.runs_01/xiaoai-voice-20261020/live_probe_bounded.json` 与 `guarded_speak.json`。没有为了演示而伪造在家/房间证据。定位脚本单独调用曾超过30秒，已记录为无法可靠取得当前在家状态，而不是擅自判定在家。

## 现有通道

- 已有设备：小爱音箱 Play，`hardware=LX05`。
- MiNA `deviceID=d547528a-bdbc-4784-ac8d-ba20ba99e5fa`，用于 TTS 和 ubus 音量。
- MIoT `DID=445724538`，用于独立读取音量 `(2,1)`。不能混用两种 ID。
- 已有 venv：`/Volumes/扩展硬盘/hermes/.hermes/tools/xiaoai-venv`；本次核验 `miservice==3.0.1`。
- 设备不在 Mac 同一局域网，走小米云，不假设本地 MiIO 可达。
- 凭据：`/Users/sober/.mi.token`，已存在，权限0600；仅记录位置，不打印内容，不提交 Git。探针使用内存 token、`token_store=None`，禁止 SDK 自动重新登录，避免 SDK 在401后删除现有 token。
- 原封装：同目录 `xiaoai_control.py`。出声**只**走 `xiaoai_say.sh "文本"`：先 ubus 音量设20，再独立 MIoT 回读确认≤25，才单设备 TTS。不会使用原生 CLI 的全设备广播。
- 所有子进程移除大小写 HTTP/HTTPS/ALL_PROXY 与 MI_USER/MI_PASS；无自动重发。

凭据需要续期时，仅让 Sir 在本机 Terminal 交互运行已有入口：

```sh
bash /Volumes/扩展硬盘/hermes/.hermes/tools/xiaoai_login.sh
```

密码、验证码不进入聊天，也不写入项目文件。不要在定时任务内自动跑登录。

## API 和门控

```python
from voice_xiaoai import XiaoAIError, is_available, speak

try:
    online = is_available()  # 离线/设备不在账号中：False；凭据/云服务故障：异常
    result = speak("Sir，有一条新的提醒。", context=observed_context)
except XiaoAIError as error:
    # error.code 是可路由诊断；error.status == "unknown" 时禁止自动重发
    handle_failure(error.code, error.status)
```

`observed_context` 由**可信的实时感知层**提供，不是模型随手填写，不是固定配置：

```json
{
  "state": "home",
  "pipeline_ok": true,
  "stale": false,
  "last_point_ts": "<带时区的真实定位时间>",
  "room": "卧室",
  "room_observed_at": "<带时区的真实房间观测时间>"
}
```

`device_routing.DeviceContext.xiaoai_context` 接收同一份观测字典；`select_output_device(..., now=...)` 在小爱分支强制执行门控，缺少证据时回退其他设备，不依赖 Tailscale/Mac 活跃启发式证明可出声。在此基础上 `speak()` 再次门控：

- 必须明确在家、定位新鲜、管道正常；定位时间距今小于90分钟且不是未来。
- 必须实际观测在卧室/客厅（兼容 `bedroom` / `living_room`）；房间观测小于5分钟且不是未来。
- 中国时区23:00–08:00静默。
- 真正下发前再查询绑定设备在线，确认音量≤25；文本1–2000字符，不接受空白/NUL。
- 请求超时或回执不完整标记 `unknown`；绝不自动重发，防止重复讲话。

无 `context` 时调用现有 `.hermes/scripts/presence.py --json`，但 GPS 本来就不能判断卧室/客厅，**默认保持静音**。`presence_fused.py` 已退役，不调用。当前没有自动房间传感器，不能把“在家”偷换成“在覆盖范围”。Tailscale/Mac活跃启发式也不能替代此门控。

同步 API 在异步网关中需放入工作线程。调用有界子进程；超时终止整个 shell/SDK 进程组。`XiaoAIError.code` 包括 `credentials_invalid`、`device_offline`、`service_unavailable`、`transport_missing`、`volume_unverified`、`presence_blocked`。错误不输出原始云响应、凭据或敏感播报文本。

成功仅返回：`{"device_id": "…", "status": "submitted", "heard": false}`。由 Sir 确认听到后，另记人工确认，不能改写此前日志冒充已验证。LX05 使用小米自身 TTS 声音；本实现不提供自定义音色、流式音频、可靠播放结束回执或打断。

## 反向监听调研结论

**把未改装 LX05 直接当作 JARVIS 常开麦克风，持续监听并转发 Sir 的所有讲话：按当前官方公开接口不可行。** 未发现官方持续原始麦克风流或全量 ASR 推送接口；这不是对破解固件等所有可能性的绝对否定。

1. [小爱技能 HTTPS 协议（官方）](https://developers.xiaoai.mi.com/documents/Home?type=/api/doc/render_markdown/SkillAccess/skill/fulu/HTTPS)：仅当请求路由到开发者技能时，小爱经过语义理解后发送 HTTPS 请求（意图、槽位、可选 query）。`open_mic` 表示技能内续轮，不是开放持续原始音频。需技能配置/验签及上线审核，不能截获全量日常会话。可单独立项做“唤醒指定技能→JARVIS webhook”。
2. [小爱 Android 语音 SDK（官方）](https://developers.xiaoai.mi.com/documents/Home?type=/api/doc/render_markdown/VoiceserviceAccess/Device/develop/SDKDocument/AndroidInstruction)：`postData()` 是开发者终端上传自己采集的音频，不是读取现成 LX05 的麦克风。单次 ASR 时长限制也不是持续监听接口。
3. [xiaogpt 公开源码](https://github.com/yihong0618/xiaogpt/blob/main/xiaogpt/xiaogpt.py)：账号态轮询 `userprofile.mina.mi.com/device_profile/v2/conversation`，读取小米云已识别的历史文字，并按时间去重。当前实现取最近记录，可能延迟/漏条，私有接口、风控及固件兼容性均不稳定；不等同实时推送、全量 ASR 或持续音频。本任务没有启用历史轮询，也没有读取 Sir 的历史语音。
4. [MiService](https://github.com/Yonsm/MiService/blob/main/miservice/minaservice.py) 的 `get_latest_ask()` 调用 `mibrain/nlp_result_get`，同样只是既有 NLP 问答结果，不是麦克风订阅。

可靠的反向入口应继续使用 Mac/iPhone 自有麦克风；若做指定技能 webhook，另行说明技能调用范围、授权和失败回退，不宣称“小爱已变成 JARVIS 常开双向语音终端”。

## 验证

```sh
PYTHONPATH=packaging ./.venv/bin/python3 -m pytest packaging/test_voice_xiaoai.py -q
./.venv/bin/python3 -m ruff check packaging/voice_xiaoai.py packaging/test_voice_xiaoai.py
./.venv/bin/python3 -m pyright --level error packaging/voice_xiaoai.py
```

单元测试 mock 网络/子进程，覆盖设备绑定、凭据/服务/离线分类、音量回读契约、在家与房间门控、夜间/过期/未来观测、输入校验、未知结果不重发、进程组超时清理。它们不证明真实设备出声。
