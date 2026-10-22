# 传输隔离实验主脑验收裁决归档

确认时间：2026-10-15T09:59:46+0800（本机真实 date）。本文件仅机械登记本轮主脑裁决与只读核账结果，未执行新实验或产品测试。

## 裁决与授权

Sir「批准，仅开展隔离实验」范围内的本轮验收完成，接受 `PASS_LOCAL_EXPERIMENT`；不接受 `PASS_OS_ISOLATION`，不代表正式产品或生产通过。架构方向具备局部实测支持，可作为后续设计输入。

授权真源：`/Volumes/扩展硬盘/jarvis/docs/decisions/OWNER_AUTHORIZATION_TRANSPORT_EXPERIMENT_20261015T094342+0800.md`；SHA-256 `5b2bb57d2c2573c59c5818d417a58978cddd766c272f01048363bdd1a2c31034`。授权与原提案、作者和独立验收原件保持只读；历史 pending 状态是当时时点，不回写。

## 证据绑定与本轮核账

独立证据根：`/Volumes/扩展硬盘/jarvis/.runs_01/credential-broker-transport-acceptance-20261015/`。

|原件|本轮实际 SHA-256|
|---|---|
|REPORT.md|`9df10b60a5c4cda1a9708326ea098c7aa6c72dbaca7c010e6d79250972709cd1`|
|FINAL_RESULTS.json|`26eb18b79f2faea60a9bd422c92bc680d1420d5d4dc13aabf46f91a6d7095500`|
|MANIFEST.json|`4ada1b9bfc5d8651676a5ef1f92860702e76d253fce87c9b2090bf6701794f42`|

作者原件：`/Volumes/扩展硬盘/jarvis/.runs_01/credential-broker-transport-isolated-20261015/`；MANIFEST.json SHA-256 `f6e6a3aa9e8de35316a9e5f372f4de1dc65df603f89bb8dd93008cdad2b86670`。

本轮只读重算独立清单 34/34 项 SHA-256 与大小一致，作者清单 24/24 项 SHA-256 一致；两目录均仅清单自身未列入。解析最终 JUnit 与 FINAL_RESULTS：原测试 10 通过，独立测试 8 通过，故意阻塞 controller 的负控 1 FAIL、0 ERROR，正向亦无 failure/error/skip。负控恰为屏障关闭时 control 无响应的目标断言失败，是鉴别力证据；不能抹掉，也不能当成产品失败。最终 Pyright strict 为 4 files/0 errors，Ruff exit 0；这些是封存实跑证据读回，本次未重跑。

## 接受的局部行为

独立报告及绑定事件支持：transport 已进入独立 sink，release 屏障尚未放开且 transport 仍存活时，control revoke/query 已答；撤销后拒绝新接纳，晚成功仍归原 operation。crash/EOF/timeout 转 unknown，重复请求不换 operation、不重发，unknown 后晚回不改成功。负控使用相同屏障响应断言，保留真实 FAIL。

独立清理记录覆盖两轮 30 个实例的 90 个子进程及 6 个 Loop 子进程，共 96 个不同记录 owned PID，均 reap，controller threads joined，owned groups 为空。无持续服务的结论仅限该实验 observed 集合；本次只解析封存记录，不作当前全系统进程或服务审计，也不外推未来孙进程清理能力。

## 保留的边界与历史

control/controller 仍持有假 secret 的 HMAC/逐字节比较副本。worker 的 argv/env/stdin、0..255 FD 和已声明 IPC 仅有限采样；broker 仅 OS argv/env/FD 元数据采样。不能宣称完整内存保密或同 UID 的 OS 隔离。

仍为同 UID、Mac、单 operation、内存状态实验；无 Linux/ECS、独立 UID、seccomp、持久撤销/epoch、跨重启、真实 provider 或真实凭据验证；多 operation、启动中途失败和资源耗尽亦未验收。无正式产品集成或生产批准。

原产品 key FD 仍给模型进程，产品没有改，正式凭据隔离没有上线。原 typed 样机 3×UP040+1×UP047 四个 Ruff 错误及旧同步 hang 历史继续保留；新获准架构实验的通过不等于旧样机修复通过。作者与独立验收的首轮失败、工具配置错误和后续绿色证据原样保留，不改写历史。

## 后续与门禁

真实凭据或网络接线、正式产品集成须另给精确变更对象，并经独立审查和相应批准；不得借本实验批准扩大权限，本分支不自动进入真实凭据接线。现有完整 JARVIS 开发授权及非此分支工作不被取消。

K 状态及原用户验收确认位、`PREPARATION_IN_PROGRESS / production NO`、Contract Pack `NOT_FROZEN`、DP/ADR/正式核心协议 `NOT_APPROVED`、Linux/ECS 实机 `NOT_RUN` 保持；旧 cron/邮件/看门狗不恢复。

本次仅新建本裁决档并在两份状态顶部追加短指针；保留全部历史。未改实验/代码/配置/密钥，未调用模型或网络，未重启、部署或 Git 提交；不把未测项目写为通过。