# ADR-0011 结果未知时默认拒绝新发送，产品链路显式放行并留痕

状态：已实施（2026-10-21）
相关：`packaging/chat_launcher.py`

## 背景

模型调用可能处于"结果未知"状态：子进程被信号杀死、写入中断、超时但对方可能已收到请求。
这时如果直接发下一次调用，可能重复产生副作用（重复发消息、重复执行命令）。
仓库里原本有 8 条测试断言"结果未知后不得再派生新进程"，但代码只做了按 attempt 的幂等，
新 prompt 会生成新 UUID 直接放行，所以测试一直红。

## 决策

1. `launch()` 默认 `after_unknown="refuse"`：数据根上存在 started/unknown 且未核对的调用时，
   拒绝任何新发送，报错给出 attempt ID 与解除方式。
2. 新增 `unresolved_attempts()` / `resolve_attempt()`：人工核对后标记 resolved，
   不重发、不伪造结果。
3. 产品侧显式放行：语音大脑、reflection、absorb、affect_engine 传 `after_unknown="continue"`，
   每次放行写 `launch_after_unknown_continue` 警告日志，保留事后核对线索。

## 理由

- 语音是聊天轮次，不产生外部副作用；让一次网络抖动把助手永久变哑，代价高于收益。
- 但只要还有工具执行能力，"未知即禁止"必须是可以恢复的默认，不能靠调用方自觉。

## 代价

- 库的默认行为变严格：新代码若在未知后继续调用，会收到 LauncherError。
- 语音链路在未知后仍会继续，靠日志留痕事后追查；正式开放工具执行前，
  需要把这条改成"未知时先向用户确认"。

## 验证

`packaging/test_chat_launcher.py` 36 passed（原 8 failed）；测试断言改为比较
"第二次发送前后"的派生次数——一次 attempt 内部允许热引擎失败后回落冷启动（两次 Popen），
但结果未知后必须是 0 次。
