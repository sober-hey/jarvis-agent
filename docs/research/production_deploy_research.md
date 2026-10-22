# JARVIS 单机 Linux 生产部署与运维调研

> 目标形态：一台独立 Linux 云主机长期常驻，三部分组成——
> ① Python 智能体守护进程（内心循环、模型调用）；② asyncio 语音网关（WebSocket:8765，Apple 设备直连）；③ 记忆库 SQLite。
> 现状约束：模型 API key 在 `~/.jarvis` 下的 0600 文件；设备侧共享 Bearer token + 明文 `ws://`（内网/Tailscale）；Web 面板只允许 loopback。
>
> 结论先行：**不要照搬 macOS 的 launchd 心智模型**。systemd 的三个坑（`StartLimitIntervalSec` 放错段被静默忽略、`KillMode=mixed` 留孤儿进程、`Type=notify` 不发 `READY=1` 卡在 activating）会让"看起来在跑"的服务实际不可用。下文每一项都给了可直接抄的配置。

---

## ① 六个子题：机制 + 推荐配置 + 来源

### 1.1 systemd unit 最佳实践（长驻 Python 服务）

**机制要点。** systemd 管一个长驻服务，真正决定可靠性的不是 `Restart=always`，而是四组参数的配合：

- **`Type=` + `NotifyAccess=`**：`Type=notify` 让 systemd 等到进程发 `READY=1` 才认为启动完成，`systemctl start` 会阻塞到那一刻。这对 JARVIS 很重要——模型预热、SQLite 迁移、网关 bind 都完成后才算 ready，否则依赖它的单元会在半初始化状态下启动。代价：如果进程永远不发 `READY=1`，单元会卡在 `activating` 直到 `TimeoutStartSec`（默认 90s）然后以 `Result: timeout` 失败。`NotifyAccess` 在 `Type=notify` 下不写时隐式等于 `main`，**只有主进程能发通知**；子进程（比如 model worker）发的 `WATCHDOG=1` 会被丢弃，除非设 `NotifyAccess=all`。
- **`WatchdogSec=`**：systemd 通过 `WATCHDOG_USEC` 环境变量告知期望间隔，进程需在读到的间隔的一半以内发 `WATCHDOG=1`。超时未收到，systemd 用 `SIGABRT` 杀掉（留下 core 便于诊断），`Restart=on-watchdog` 触发重启。**关键陷阱**：如果用独立线程定时 ping，事件循环被阻塞时线程照样在 ping，watchdog 就完全失效了。对 asyncio 服务，ping 必须由事件循环内的 task 发出，让"循环卡死 ⇒ 停止 ping"成立。
- **`KillMode=`**：默认 `control-group` 是正确的。`mixed` 只给主进程发 `SIGTERM`，随后给 cgroup 剩余进程发 `SIGKILL`，且已知与 `ExecStopPost=` 同用时会在主进程按时退出的情况下**漏杀**（systemd issue #14566）。JARVIS 会派生子进程（模型调用、浏览器、TTS），一旦有孤儿留在 cgroup 里，会出现 `Failed to kill control group` 导致单元进入 failed 而 `Restart=` 不生效——Hermes 自己就踩过这个坑（issue #1617：Chrome 子进程留在 cgroup，`KillMode=mixed` 只 SIGKILL 主进程，服务停在 failed 需手工拉起）。**结论：`KillMode=control-group` + `SendSIGKILL=yes` + 合理 `TimeoutStopSec`**。
- **`Restart=` 与启动限速**：`Restart=` 取值 `no/on-success/on-failure/on-abnormal/on-watchdog/on-abort/always`。**`Restart=always` 并不真的"总是"**——它受启动限速约束。`StartLimitIntervalSec=` / `StartLimitBurst=` 自 systemd v230 起属于 **`[Unit]` 段**，写在 `[Service]` 段会被静默忽略（只打一条 warning，限速实际关闭，崩溃循环不会被拦住）。这是最常见的复制粘贴错误。另外限速能否触发取决于数学关系：`StartLimitBurst × (RestartSec + 存活时间) < StartLimitIntervalSec`，默认窗口 10s 配上 `RestartSec=5` 意味着**永远不触发**，等于无限重启。

**推荐配置。** 见 ② 节的完整 unit。要点摘录：

```ini
[Unit]
StartLimitIntervalSec=300      # 必须在 [Unit]
StartLimitBurst=5

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=120                # 模型调用可能很慢，给宽一点
Restart=on-watchdog            # 被 watchdog 杀 → 重启；再配 RestartSec
RestartSec=10
TimeoutStartSec=180            # 模型预热/DB 迁移可能超过默认 90s
TimeoutStopSec=45              # 给 SQLite 收尾 + WS 优雅关闭留时间
KillMode=control-group         # 不要用 mixed
SendSIGKILL=yes
```

**沙箱化。** `ProtectSystem=strict` 把整个文件系统挂成只读（`/dev` `/proc` `/sys` 除外），配合 `StateDirectory=` 等由 systemd 自动创建并授权的可写目录使用；`ProtectHome=yes` 让 `/home` `/root` `/run/user` 对服务不可见——服务代码放 `/opt` 就完全不受影响。`NoNewPrivileges=yes` 禁止通过 execve 提权。`RestrictAddressFamilies=` 收窄到实际需要的协议族（WebSocket 与 DNS 需要 `AF_INET AF_INET6 AF_UNIX AF_NETLINK`）。

**关于 `DynamicUser=` 的判断（本方案不用它）。** `DynamicUser=yes` 会隐式打开 `RemoveIPC=`、`PrivateTmp=`、`NoNewPrivileges=`、`RestrictSUIDSGID=`、`ProtectSystem=strict`、`ProtectHome=read-only`，看起来很划算。但它给的是**每次启动可能变化的 UID**，JARVIS 有两个硬约束与它冲突：
1. 记忆库 SQLite 与 0600 密钥文件必须落在**稳定路径、稳定属主**上，备份工具（restic/borg）依赖属主一致才能正确去重与恢复；
2. 代码里若残留 `Path.home() / ".jarvis"` 这类硬编码，`DynamicUser` 下 home 是 `/`，路径直接失效。

所以本方案用**静态系统用户 `jarvis`**，然后**显式把 DynamicUser 隐式带来的那几项全部手写出来**，拿到同样的沙箱强度而保留稳定 UID。

**依赖隔离。** 用 `uv`：`uv.lock` 是跨平台精确锁文件，`uv sync --locked --no-editable` 拒绝过期锁文件、保证生产环境与锁文件一致。生产环境**不要在启动时同步依赖**——unit 里直接 `ExecStart=/opt/jarvis/.venv/bin/python -m jarvis.daemon`，把 `uv sync` 放进部署脚本。这样重启不依赖网络和 PyPI 可用性。

**日志。** 见 1.2。

**来源**
- systemd.service（`Type=`、`WatchdogSec=`、`Restart=`、`NotifyAccess=`、`TimeoutStartSec`）：https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
- systemd.kill（`KillMode=` 四种取值的精确语义、`SendSIGKILL=`）：https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html
- systemd.exec（`ProtectSystem=`/`ProtectHome=` 精确语义；`DynamicUser=` 的隐式项清单）：https://manpages.ubuntu.com/manpages/focal/man5/systemd.exec.5.html
- `StartLimitIntervalSec` 属于 `[Unit]`、放 `[Service]` 被忽略：https://github.com/freenet/freenet-core/issues/4551 ；限速数学关系与 `RestartSec` 交互：https://hvr.biz/topic.php?slug=systemd-start-limits-why-your-service-stopped-restarting
- `KillMode=mixed` + `ExecStopPost=` 漏杀：https://github.com/systemd/systemd/issues/14566
- 孤儿子进程导致 failed、需 `KillMode=control-group` 的真实案例：https://github.com/NousResearch/hermes-agent/issues/1617
- `Type=notify`/watchdog 实现细节与坑（`@` 抽象 socket、半间隔 ping、`TimeoutStartSec` 与 watchdog 分工）：https://oneuptime.com/blog/post/2026-03-02-how-to-configure-systemd-watchdog-for-service-health-checks-on-ubuntu/view
- uv 锁文件与 `--locked`：https://docs.astral.sh/uv/concepts/projects/sync/
- 沙箱化配置组合参考（Red Hat）：https://www.redhat.com/en/blog/systemd-secure-services

### 1.2 日志：journald 还是文件轮转

**机制。** journald 自带按大小轮转，无需 `logrotate`。两条硬约束：

- **容量**：`SystemMaxUse=` / `SystemKeepFree=` 取两者中更小者生效；`SystemMaxUse=` 默认是文件系统的 10%（上限 4G）。journald **只删归档文件**，不会为了回到配额去删活动文件，所以超配额后实际占用可能仍高于设定值。
- **限速**：`RateLimitIntervalSec=` / `RateLimitBurst=` 默认 `30s` / `10000`，且**按服务独立计数**，多服务互不干扰。超限的消息被直接丢弃——对"启动风暴"友好，对"排障时日志消失"致命。JARVIS 的内心循环若高频打日志，必须调高或显式关掉本单元的限速：`LogRateLimitIntervalSec=` / `LogRateLimitBurst=` 可写在 `[Service]` 里覆盖全局。

**推荐。** **全部走 journald，不写文件**（除了需要结构化审计的少量事件）。理由：轮转、压缩、按单元索引、`journalctl -u jarvis-agent -f` 开箱即用，且不需要为 `/var/log` 单独做权限与轮转配置。Python 侧只要 `PYTHONUNBUFFERED=1` 并打到 stdout/stderr 即可。

```ini
# /etc/systemd/journald.conf.d/60-jarvis.conf
[Journal]
Storage=persistent
SystemMaxUse=1G
SystemMaxFileSize=64M
SystemKeepFree=2G
```

```ini
# 在 unit 里给高日志量的单元单独放宽
LogRateLimitIntervalSec=30s
LogRateLimitBurst=50000
```

**唯一例外**：如果要把日志外送到别处（Loki/云端），或需要"日志被本地清掉也能查"，再加一个文件 sink + `logrotate`，`daily rotate 14 compress delaycompress missingok notifempty copytruncate`。`copytruncate` 对不重开文件的 Python 进程必要。

**来源**
- journald.conf 官方（`SystemMaxUse=`/`SystemKeepFree=` 语义、只删归档、`RateLimitBurst=` 按服务计数）：https://www.freedesktop.org/software/systemd/man/journald.conf.html
- 默认值与 `LogRateLimitBurst=` 单元级覆盖：https://sematext.com/blog/journald-logging-tutorial/
- 限速与配额实践：https://oneuptime.com/blog/post/2026-03-04-journald-rate-limiting-storage-quotas-rhel-9/view

### 1.3 内网服务的安全入口（Tailscale/WireGuard vs 反代 + TLS）

**机制与取舍。** 三条路，不是互斥而是分层：

| 方案 | 加密在哪一层 | 端口暴露 | 适用 |
|---|---|---|---|
| A. 只绑 Tailscale 接口（`100.x`）+ 防火墙只放 `tailscale0` | WireGuard（L3） | 公网 0 个 | 设备全在 tailnet |
| B. 绑 loopback + `tailscale serve` 反代 | WireGuard + Tailscale 自动签发 TLS（L7） | 公网 0 个 | 设备全在 tailnet，但客户端要求 `wss://` |
| C. 公网 443 + Caddy/Nginx 终止 TLS | TLS（L7） | 443 | 有 tailnet 外的设备/浏览器 |

**关键认知**：Tailscale 的流量本来就是 WireGuard 端到端加密的，所以"明文 `ws://`"在 tailnet 内的实际风险不是窃听，而是**① 一旦误开 Funnel 或加了公网反代，明文立刻变成真明文；② 共享 Bearer token 在 L7 明文传输，被抓包即得；③ 无法区分设备，一台泄露等于全部泄露**。所以不必为"加密"急着上 TLS，但必须为②③做改动。

**推荐（按优先级）：**

1. **网关只 bind `127.0.0.1:8765`，绝不 `0.0.0.0`。** 这一条挡掉绝大多数事故——云主机上 `0.0.0.0:8765` 会在几分钟内被扫到。
2. **用 `tailscale serve` 提供 `wss://`**，不需要额外守护进程，自动签发并续期证书：
   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:8765
   # 设备侧连 wss://jarvis.<tailnet>.ts.net/   （证书自动续期）
   ```
   注意：`serve` 要求 tailnet 开启 HTTPS Certificates；**不要**用 `tailscale cert` 手工拿证书——那样续期要你自己做，而 `serve`/Caddy 集成是自动续期。设备若必须留在 tailnet 外，改用 `tailscale funnel`（等于把服务暴露到公网，此时明文 `ws://` 的风险立刻成立，必须同时上 TLS + 换掉共享 token）。
3. **需要公网或按路径路由时用 Caddy 而不是 Nginx**：Caddy 的 `reverse_proxy` 自动识别并透传 WebSocket 升级，不用手写 `Upgrade`/`Connection` 头，自动 HTTPS。Nginx 需要显式配置（见下），且默认 `proxy_read_timeout 60s` 会**杀掉空闲 WebSocket**——表现为"连上 60 秒后无声断开"。

**Nginx 若要上（备用）：**
```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
location /ws {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;   # 不要写 $http_connection
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}
```
三个经典错误：漏 `proxy_http_version 1.1`（默认 1.0 没有 Upgrade，握手在上游静默失败）；用 `$http_connection` 而非固定 `"upgrade"`（客户端发 `keep-alive, upgrade` 会被上游拒绝）；忘调 `proxy_read_timeout`（60s 断连）。

**心跳（三层都要有）：**
- **应用层**：`websockets` 库默认每 20s 发 Ping 帧、等 Pong，`ping_interval` / `ping_timeout` 可调。这是最可靠的一层，因为它同时测了延迟（`websocket.latency`）。
- **反代层**：`proxy_read_timeout` 设 3600s，让反代不抢在应用心跳之前断。
- **设备层**：iOS/watchOS 在后台会挂起 socket，重连是常态而非异常。网关必须把"设备断开"当正常事件，不能当告警；重连风暴要有退避。

**来源**
- Nginx WebSocket 官方（`Upgrade` 是 hop-by-hop 头，必须显式透传；默认 60s 无数据即断，需 `proxy_read_timeout` 或后端 ping）：https://nginx.org/en/docs/http/websocket.html
- Nginx 三个经典错误与 `Connection` 变量陷阱：https://stackharbor.com/en/knowledge-base/nginx-websocket-proxying
- Caddy `reverse_proxy` 官方：https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- `tailscale serve`（自动 HTTPS、仅支持 `http://127.0.0.1` 后端、与 Funnel 端口互斥）：https://tailscale.com/docs/reference/tailscale-cli/serve
- Tailscale 证书自动续期 vs `tailscale cert` 需手工续期：https://tailscale.com/docs/how-to/set-up-https-certificates
- 只放 `tailscale0`、关公网 SSH、`fail2ban` 变成可选：https://wihan.dev/writing/securing-your-linux-vps-with-tailscale
- `websockets` keepalive（默认 20s ping、延迟测量）：https://websockets.readthedocs.io/en/stable/topics/keepalive.html

### 1.4 密钥管理：从明文 env 文件到系统密钥环

**机制。** 先把威胁模型摆正，否则会买错工具：

> 在单台自管主机上，**root 能读一切**。任何方案的"加密"保护的是**磁盘被偷/快照泄露/备份仓库泄露**，不是"防住 root 或防住本机其他同 UID 进程"。所以排序应该是：**① 别进 git ② 别进镜像层 ③ 别进 shell history ④ 别进 `/proc/<pid>/environ` ⑤ 最后才是静态加密**。

`Environment=` / `EnvironmentFile=` 的问题正在 ④：它们对同 UID 进程可读（`/proc/<pid>/environ`），且会被**所有子进程继承**——JARVIS 派生的模型子进程、TTS 子进程、浏览器都会拿到明文 key。这是本方案里最该修的一条。

**最小可行升级路径（四步，可分批做）：**

**第 1 步（今天就能做，收益最大）：`LoadCredential=`。** systemd 在激活时把文件复制进一个**每单元私有**的目录，文件 mode `0400`、目录 `0700`，通过 `$CREDENTIALS_DIRECTORY` 暴露；不进环境变量、不进 `/proc/environ`、不向下进程树传播，且凭据放在不可交换内存（ramfs）里。

```ini
[Service]
# 源文件仍是你现在的 0600 文件，但服务只拿到一份私有投影
LoadCredential=model-api-key:/etc/jarvis/secrets/model-api-key
Environment=JARVIS_MODEL_KEY_FILE=%d/model-api-key   # %d = 凭据目录
ExecStart=/opt/jarvis/.venv/bin/python -m jarvis.daemon
```
应用侧改成读**文件路径**而不是读环境变量里的值。这样即使子进程继承了环境，也只继承一个路径。

**第 2 步：`LoadCredentialEncrypted=`（静态加密）。** 用 `systemd-creds` 以 AES-256-GCM 加密，密钥绑定本机 `/var/lib/systemd/credential.secret` 和/或 TPM2：

```bash
sudo install -d -m 0700 -o root -g root /etc/credstore.encrypted
sudo systemd-creds setup                      # 无 TPM 的云主机上强制用 host key
printf '%s' "$MODEL_API_KEY" | sudo systemd-creds encrypt --with-key=host \
  --name=jarvis-model-api-key - /etc/credstore.encrypted/jarvis-model-api-key.cred
sudo chmod 0600 /etc/credstore.encrypted/jarvis-model-api-key.cred
# 验证能解回来（在 systemd 上下文里，不是直接 cat）
sudo systemd-run -P --wait -p LoadCredentialEncrypted=jarvis-model-api-key \
  systemd-creds cat jarvis-model-api-key | wc -c
```
unit 里换成 `LoadCredentialEncrypted=jarvis-model-api-key`。**解密失败会让单元启动失败**（这比静默拿到空字符串好）。**注意**：`/var/lib/systemd/credential.secret` 丢了 = 这些凭据全废，必须和加密备份分开保管。

**第 3 步（可选，为 git 可管理）：sops + age。** 只有当你希望配置文件（含非密钥配置）进 git 时才需要。`sops exec-env secrets.enc 'cmd'` 只把值注入指定子进程，不落明文文件——比 `sops -d > .env` 好，后者会在磁盘上留明文。**age 私钥必须离线托管**（U 盘/密码管理器），丢了就永久解不开。

**第 4 步（仅当需要隔离 + 预算控制）：本地 sidecar 代理。** 如果目标是"模型子进程不该拿明文 key"，真正干净的做法不是把 key 传下去，而是**根本不下发**：

```
JARVIS agent ──(虚拟 key, 仅 loopback)──> LiteLLM proxy ──(真 key, LoadCredential)──> 上游模型 API
```
上游 key 只存在于 proxy 进程；agent 拿到的是一个可撤销、带 `max_budget` / 模型白名单 / 过期时间的虚拟 key。收益不只是"少一处明文"，还包括：**按模型/按用途的花费上限、限流、以及 key 泄露时只吊销虚拟 key 不用换上游**。代价是多一个进程要监控。

**不要做的事：** 单机场景不要上 Vault——它是平台不是工具，动态密钥/租约/审计才值它的运维成本。也不要相信"Docker secrets 是密钥管理"：普通 compose 下它就是 bind mount，无静态加密。

**来源**
- systemd CREDENTIALS 设计文档（激活时获取、不进进程树、内核级访问检查、不可交换内存）：https://github.com/systemd/systemd/blob/main/docs/CREDENTIALS.md
- `LoadCredential=` 与 `%d` / `${CREDENTIALS_DIRECTORY}` 用法、`Environment=` 会出现在 `/proc/<pid>/environ`：https://github.com/zcecc22/systemd-manual/blob/main/docs/14-environment-and-credentials.md
- `systemd-creds encrypt --with-key=host`、`/etc/credstore.encrypted/`、解密失败即启动失败、host key 备份警告：https://dev.to/lyraalishaikh/stop-putting-secrets-in-environment-variables-practical-systemd-creds-on-linux-4gfi
- sops + age 的适用边界与 `exec-env` 优于 `-d > file`：https://gethasp.com/guides/sops-for-one-person-teams/
- 单机威胁模型排序（root 能读一切；git 才是真泄露点）：https://systhoughts.com/posts/docker-secrets-arent-really-secrets
- LiteLLM 虚拟 key / 上游 key 不出网关：https://docs.litellm.ai/docs/proxy/virtual_keys

### 1.5 备份：3-2-1 与可恢复性验证

**机制。** SQLite 在 WAL 模式下**不能用 `cp`/`tar` 备份**——会漏掉已提交但还在 WAL 里的数据，或抓到写了一半的页。它"大多数时候能work"，这正是危险之处。三种安全做法：

| 方式 | 一致性 | RPO | 适用 |
|---|---|---|---|
| `sqlite3 db ".backup out"` | 在线安全（SQLite backup API） | = 备份间隔 | 默认选择 |
| `VACUUM INTO 'out'` | 单事务快照，且顺带整理碎片 | = 备份间隔 | 写入频繁时更好 |
| Litestream | 流式复制 WAL | ~1 秒 | 需要 PITR |

**推荐架构（两段式，别混）：**

```
live data.db ──.backup──> /var/backups/jarvis/staging/ ──restic──> 异地对象存储
   (cron/timer)             (一致的、可直接打开的 .db)              (加密+去重)
```
关键点：**restic 永远不碰活的 DB**，只碰 staging 里那份一致的快照。这样"做一致快照"和"送异地"解耦，各自可单独重试。

**3-2-1 在本场景的具体落地：**
- **3 份**：生产库 + 本地 staging/日快照 + 异地仓库。
- **2 种介质**：云主机本地盘 + 对象存储（B2/S3）。别把异地副本放在同一家云同一 region。
- **1 份离线/不可变**：restic 仓库开 **append-only**（或对象存储 Object Lock），这样即使主机被拿下、攻击者拿到 restic 密码，也删不掉历史快照。

**必须先做的一件事：`PRAGMA integrity_check`。** 在送异地之前校验 staging 副本，`ok` 才继续。这一步大多数人跳过，它能在污染备份历史之前抓到坏快照。

**restic 保留策略**（推荐值）：
```bash
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-last 3 --prune
```
`forget` 只标记，`prune` 才真正回收空间；`prune` 期间仓库加锁、备份无法完成，所以别每次备份都 prune（每周一次）。`check --read-data-subset=1G/10` 每周轮换校验 1/10，十周覆盖全仓，避免一次性拉全量。

**Litestream（如果要 PITR）——0.5 版的配置与 0.3 不兼容，照旧教程抄会静默失效：**
```yaml
# /etc/litestream.yml
dbs:
  - path: /var/lib/jarvis/memory.db
    replica:
      url: s3://bucket/jarvis
      region: us-east-1
snapshot:
  interval: 1h
  retention: 168h        # 默认只有 24h！周三发现的坏迁移，周一的状态已经恢复不了
```
- 字段是 **`replica`（单数）**，0.3 的 `replicas` 数组会让启动直接失败。
- `retention` 是**全局 `snapshot` 块**下的设置，写在 `replica` 里会被**静默忽略**并停在 24h 默认值。
- 应用必须设 `PRAGMA busy_timeout = 5000`，否则 Litestream checkpoint 抢写锁时应用报错。
- 只支持 WAL 模式；**不要在复制运行时跑 `VACUUM`**（用 `VACUUM INTO`）。
- 绝不允许两个 Litestream 进程对同一 DB/同一目标复制，否则副本不可恢复。

**恢复演练的最低标准（这是"备份存在"和"备份可用"的分界）：**

| 项 | 最低要求 |
|---|---|
| 频率 | 每月 1 次，且**每次改动备份脚本后立即做 1 次** |
| 动作 | 从异地仓库 `restic restore` 到临时目录（不是原地） |
| 校验 | `PRAGMA integrity_check` = `ok`；关键表行数与生产对比；用应用代码真的打开它读一次 |
| 计时 | **记录 time-to-restore**（RTO）。没测过时间的恢复计划等于没有计划 |
| 记录 | 结果写进 `RUNBOOK.md`：日期、快照 ID、耗时、校验输出 |
| 告警 | 备份脚本非零退出 → 告警；**超过 N 小时没成功 → 死信告警**（dead-man switch） |

**恢复时最容易忘的两步**（会导致"恢复了但还是坏的"）：
1. 先删掉残留的 `-wal` / `-shm`。旧 WAL 会被应用到干净恢复的库上，重新弄坏它。
2. 修正属主。root 拥有的库文件会让服务静默打不开。

**还要备份的非数据库资产**（Litestream 和 `.backup` 都覆盖不到）：unit 文件、`/etc/jarvis/`、`Caddyfile`、`/etc/credstore.encrypted/`、`/var/lib/systemd/credential.secret`（若用了 host-bound 凭据）、tailscale state、**age 私钥（离线，且不能只存一份在主机上）**。

**来源**
- `.backup` vs `VACUUM INTO` vs Litestream 的选择与 `cp` 为何危险：https://www.bigiron.cc/guides/the-realistic-sqlite-backup-pattern-for-a-self-hosted-app
- Litestream 官方 cron 方案（"不要用 `cp`"、`integrity_check`、dead-man 服务）：https://litestream.io/alternatives/cron/
- Litestream 0.5 配置形态（`replica` 单数、`snapshot` 全局块、retention 默认 24h、`busy_timeout`、禁 `VACUUM`、禁双实例）：https://www.ssdnodes.com/learn/sqlite-in-production-vps
- Litestream Tips（`busy_timeout=5000`、WAL、`synchronous=NORMAL`、1 秒数据丢失窗口）：https://litestream.io/tips/
- restic `forget`/`prune` 语义与 `--keep-*` 按自然时间边界计算：https://github.com/restic/restic/blob/master/doc/060_forget.rst
- restic 子集校验与维护 timer 写法：https://sumguy.com/restic-repo-maintenance-prune-check-forget

### 1.6 监控告警最小集

**机制与最关键的认知。** 单机常驻服务的失败模式不是"进程挂了"——`Restart=` 已经处理了那个。真正会咬人的是：

1. **进程活着但功能死了**（事件循环死锁、DB 锁住、上游 key 失效）。systemd 视角是 `active (running)`。
2. **重启循环**：每 90 秒崩一次再起来，`is-active` 永远返回成功，`NRestarts` 才是真相。
3. **静默失效**：备份没跑、cron 没触发、告警通道本身断了。这类失败**没有事件可捕获**，只能用"预期心跳没来"来发现（dead-man switch）。
4. **资源渐近**：磁盘被日志/快照填满、RSS 缓慢泄漏。到阈值时往往已经晚了。

**选型：单机场景先用 cron+脚本+死信服务，别上 Prometheus。** 理由：Prometheus + node_exporter + alertmanager 是三个常驻进程 + 一个 TSDB，为了 5 个阈值指标付出的运维成本超过收益；而它**本身也需要被监控**（alertmanager 挂了没人知道 = 所有告警静默失效）。什么时候该升级：需要历史曲线做容量规划、指标数超过 ~20 个、或需要多主机聚合时。

**最小集（五个探针，缺一不可）：**

| # | 探针 | 抓什么失败 | 实现 |
|---|---|---|---|
| 1 | **黑盒 WS 探活** | 进程活着但网关不接连接 | 真做一次 WebSocket 握手 + 一帧 echo，不是 `nc -z` |
| 2 | **心跳文件 mtime** | 内心循环卡死（进程在但不再思考） | agent 每轮 `touch /run/jarvis/heartbeat`；探针判 `now - mtime > 3×周期` |
| 3 | **`NRestarts` / uptime** | 重启循环被 `is-active` 掩盖 | `systemctl show -p NRestarts -p ActiveEnterTimestamp` |
| 4 | **磁盘 + RSS 阈值** | 日志/快照填满盘；内存泄漏 | `df` 使用率 > 85% 告警；RSS > `MemoryHigh` 的 90% 预警 |
| 5 | **备份/告警链路死信** | 备份没跑；告警发不出去 | 备份成功后 ping healthchecks；**另设一个"告警链路心跳"** |

**第 5 项的双层设计很重要**：`alertmanager` 生态里的标准做法是让一个 **Watchdog 告警永久处于 firing 状态**，路由到一个外部心跳服务；心跳停了就说明"告警管道本身坏了"。单机脚本版等价物：探针脚本**每次成功都 ping 一次死信 URL**，包括"没有异常"的时候。这样脚本没跑 = 没人 ping = 告警。只报异常不报正常的监控，会在监控自己死掉时完全静默。

**失败连坐**：unit 里 `OnFailure=jarvis-alert@%n.service` 在单元进入 failed（包括 `start-limit-hit`）时触发一次性告警单元。注意 `StartLimitIntervalSec=0`（无限重启）会让单元永不 failed，此时 `OnFailure=` 永不触发，必须靠外部探针兜底——这是个二选一的取舍，不能两个都要。

**来源**
- `systemd-analyze security` 给单元打 0–10 暴露分（可用于 CI 闸门）：https://www.freedesktop.org/software/systemd/man/252/systemd-analyze.html
- dead-man switch 的原理与"告警缺席"vs"告警存在"的区别：https://sumguy.com/healthchecks-self-hosted-cron
- Watchdog 告警 + 外部心跳服务，且"每个集群单独一个心跳 URL，否则健康的会掩盖死掉的"：https://hyperping.com/blog/how-to-monitor-prometheus-alertmanager
- healthchecks 的 `/start` `/fail` 三态 ping：https://sumguy.com/healthchecks-self-hosted-cron

### 1.7 macOS → Linux 迁移清单（路径/权限/时区/字体/音频/launchd→systemd）

**launchd → systemd 对照表：**

| launchd | systemd | 备注 |
|---|---|---|
| `Label` | unit 文件名 | `com.x.jarvis` → `jarvis-agent.service` |
| `ProgramArguments` | `ExecStart=` | 必须绝对路径 |
| `RunAtLoad` | `systemctl enable` + `WantedBy=multi-user.target` | |
| `KeepAlive=true` | `Restart=always` | 但受 StartLimit 约束 |
| `ThrottleInterval` | `RestartSec=` | 默认 100ms 太激进，设 5–10s |
| `StartInterval` | `.timer` + `OnCalendar=`/`OnUnitActiveSec=` | |
| `StandardOutPath`/`StandardErrorPath` | 不设 → journald | 想留文件才配 |
| `EnvironmentVariables` | `Environment=` / `EnvironmentFile=` | 密钥用 `LoadCredential=` |
| `WorkingDirectory` | `WorkingDirectory=` | |
| `~/Library/LaunchAgents` | 系统单元 `/etc/systemd/system/` | **不要用 `--user`**，见下 |
| `launchctl load/unload` | `systemctl enable --now` / `disable --now` | |
| `launchctl list` | `systemctl status` / `systemctl list-units` | |

**为什么不用 `systemd --user`：** 用户单元依赖该用户有活跃 session 或 `loginctl enable-linger`，且 `--user` 管理器在用户登出/无 session 时的行为与系统管理器不同（部分命名空间设置在用户单元里不生效，除非配 `PrivateUsers=`）。服务器上的长驻服务用**系统单元 + 静态系统用户**，语义最直白。

**路径与权限：**

| 用途 | Linux 路径 | macOS 现状 | 权限 |
|---|---|---|---|
| 代码 | `/opt/jarvis`（只读，root 拥有） | `~/jarvis` | `0755` root:root |
| 状态/DB | `/var/lib/jarvis` | `~/.jarvis` | `0750` jarvis:jarvis |
| 配置 | `/etc/jarvis` | `~/.jarvis/config` | `0750` |
| 密钥 | `/etc/credstore.encrypted` | `~/.jarvis/*.key` 0600 | `0700` dir / `0600` file，root |
| 运行时（socket/心跳） | `/run/jarvis` | `/tmp` | `0750`，重启即清 |
| 日志 | journald | `~/Library/Logs` | — |
| 备份暂存 | `/var/backups/jarvis/staging` | — | `0750` |

`umask 0027` 兜底，避免新建文件默认 world-readable。**所有硬编码 `~` / `Path.home()` 的地方必须改成读环境变量**（`JARVIS_STATE_DIR` 等），这是迁移里最容易漏、又最晚才暴露的一类 bug。

**时区（高频事故点）：** Linux 云主机默认 **UTC**，macOS 是本地时区。影响：定时提醒、cron/timer 的 `OnCalendar`、token 过期判断、日志对时。做法：
```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl                       # 确认 Time zone 与 System clock synchronized: yes
```
unit 里再显式 `Environment=TZ=Asia/Shanghai`（防 systemd 环境与 shell 环境不一致）。NTP 必须有（`systemd-timesyncd` 或 `chrony`）——时钟漂移会让 JWT/缓存签名校验莫名其妙失败。

**字体：** 无头服务本身不需要字体。只有当你做**图片/PDF/截图渲染**时才需要，装 `fonts-noto-cjk` 并确认 `fc-list :lang=zh` 有输出。macOS 上"能显示中文"是系统自带，Linux 上不是。

**音频（这一项最容易被低估）：** 云主机**没有 `/dev/snd`**，任何"直接播放"的代码路径都会抛异常。
- **推荐**：TTS 只**渲染成文件**（wav/mp3），由设备端播放或走网关流式下发。这条路在服务器上天然可行，也是唯一不依赖硬件的做法。
- 若必须"播放"（比如本地校验音频质量）：用 PulseAudio/pipewire 的 **null sink**，或内核 `snd-dummy` 模块——但**云主机内核通常不带 `snd-dummy`**，`modprobe` 会失败，所以不要把它写进部署脚本的主路径。
- 麦克风同理：云上没有输入设备，任何"录音"功能必须改为设备侧采集后上传。

**来源**
- launchd.plist 各键语义（`KeepAlive`、`RunAtLoad`、`ThrottleInterval` 默认 10s、`StandardOutPath`）：https://www.manpagez.com/man/5/launchd.plist/osx-10.13.1.php
- launchd/systemd 概念对照与 `Restart`/`StartLimitBurst` 映射：https://cloudstreet-dev.github.io/macOS-and-Unix/part5-services/launchd-vs-others.html
- 无头 Linux 无音频设备的处理（null sink / `snd-dummy`）：https://superuser.com/questions/150678/simulating-audio-playback-on-headless-linux-server
- PulseAudio null sink 作为虚拟声卡：https://superuser.com/questions/1339038/how-can-i-can-create-a-virtual-pulseaudio-soundcard-on-linux-with-no-audio-hardw

---

## ② 可直接照抄的部署件

### 2.1 目录布局

```
/opt/jarvis/                         # 代码，root:root 0755，只读挂载点
├── .venv/                           # uv sync --locked 产物
├── src/jarvis/                      # 包
├── pyproject.toml
└── uv.lock

/etc/jarvis/                         # 配置（非密钥），root:jarvis 0750
├── jarvis.toml
└── logging.toml

/etc/credstore.encrypted/            # 密钥，root:root 0700 / 文件 0600
├── jarvis-model-api-key.cred
└── jarvis-ws-token.cred

/var/lib/jarvis/                     # 状态，jarvis:jarvis 0750（由 StateDirectory= 创建）
├── memory.db  (+ -wal, -shm)
├── memory.db.bak/
└── session/

/var/log/jarvis/                     # 仅当需要文件 sink 时（LogsDirectory= 创建）
/run/jarvis/                         # 运行时（RuntimeDirectory= 创建，停止即删）
├── heartbeat                         # 内心循环心跳（mtime）
└── gateway.sock

/var/backups/jarvis/staging/         # 备份暂存（restic 只读这里）
/usr/local/bin/jarvis-notify         # 告警发送脚本
/usr/local/bin/jarvis-backup.sh
/usr/local/bin/jarvis-probe.sh
```

一次性建库：
```bash
sudo useradd --system --home-dir /var/lib/jarvis --shell /usr/sbin/nologin jarvis
sudo install -d -m 0755 -o root   -g root   /opt/jarvis
sudo install -d -m 0750 -o root   -g jarvis /etc/jarvis
sudo install -d -m 0700 -o root   -g root   /etc/credstore.encrypted
sudo install -d -m 0750 -o jarvis -g jarvis /var/backups/jarvis/staging
```

### 2.2 智能体守护进程 unit

```ini
# /etc/systemd/system/jarvis-agent.service
[Unit]
Description=JARVIS agent daemon (inner loop + model calls)
After=network-online.target
Wants=network-online.target

# 启动限速必须在 [Unit]；写进 [Service] 会被静默忽略
StartLimitIntervalSec=300
StartLimitBurst=5

# 失败连坐：进入 failed（含 start-limit-hit）时触发告警单元
OnFailure=jarvis-alert@%n.service

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=120
Restart=on-watchdog
RestartSec=10
TimeoutStartSec=180
TimeoutStopSec=45

# 杀掉整个 cgroup（含模型子进程/浏览器），不留孤儿
KillMode=control-group
SendSIGKILL=yes
FinalKillSignal=SIGKILL

User=jarvis
Group=jarvis
UMask=0027
WorkingDirectory=/opt/jarvis

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TZ=Asia/Shanghai
Environment=JARVIS_STATE_DIR=/var/lib/jarvis
Environment=JARVIS_CONFIG_DIR=/etc/jarvis
Environment=JARVIS_RUNTIME_DIR=/run/jarvis
# 只传路径，不传密钥值
Environment=JARVIS_MODEL_KEY_FILE=%d/jarvis-model-api-key
Environment=JARVIS_WS_TOKEN_FILE=%d/jarvis-ws-token

# 生产不在启动时同步依赖：直接用锁好的 venv
ExecStart=/opt/jarvis/.venv/bin/python -m jarvis.daemon

# 由 systemd 创建并授权可写的目录（ProtectSystem=strict 下必需）
StateDirectory=jarvis
StateDirectoryMode=0750
ConfigurationDirectory=jarvis
ConfigurationDirectoryMode=0750
LogsDirectory=jarvis
LogsDirectoryMode=0750
RuntimeDirectory=jarvis
RuntimeDirectoryMode=0750
CacheDirectory=jarvis

# 密钥：激活时解密，落在 $CREDENTIALS_DIRECTORY，不进环境变量
LoadCredentialEncrypted=jarvis-model-api-key
LoadCredentialEncrypted=jarvis-ws-token

# ---- 沙箱（等价于 DynamicUser= 隐式项，但保留稳定 UID）----
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RemoveIPC=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
SystemCallArchitectures=native
# 若某扩展因此报 EPERM，先注释这两行定位，再决定是否放宽
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
# 注意：PrivateDevices=yes 会屏蔽 /dev/snd。TTS 走文件渲染时开着；
# 若将来需要访问 USB 麦克风，改为 PrivateDevices=no 并显式 DeviceAllow=

# ---- 资源上限（防 RSS 泄漏拖垮整机）----
MemoryHigh=1500M
MemoryMax=2G
TasksMax=512

[Install]
WantedBy=multi-user.target
```

> **照抄前必读（否则服务起不来）**：上面用 `Type=notify` + `WatchdogSec=`，前提是**应用真的实现了 sd_notify**。如果 JARVIS 目前没实现，`systemctl start` 会一直阻塞到 `TimeoutStartSec=180` 然后以 `Result: timeout` 失败——症状是"服务起不来但日志没报错"。
> 三种处理，按推荐顺序：
> 1. **（推荐）在应用里实现**：加 `sdnotify` 依赖，在 bind + DB 就绪后 `notifier.notify("READY=1")`，并起一个**事件循环内的 task** 每 30s 发 `WATCHDOG=1`。
> 2. **临时过渡**：改成 `Type=exec`、删掉 `NotifyAccess=` / `WatchdogSec=` / `Restart=on-watchdog`，改用 `Restart=always`，监控靠外部探针兜底（2.6 的心跳文件探针仍然有效）。
> 3. **不想改代码**：用一个 shell 包装脚本后台起应用、前台发 `systemd-notify --ready` 并循环 `systemd-notify WATCHDOG=1`——但这会让 watchdog 退化成"包装脚本活着"，**测不出事件循环死锁**，等于放弃了这个探针的价值。

### 2.3 语音网关 unit（独立进程）

```ini
# /etc/systemd/system/jarvis-gateway.service
[Unit]
Description=JARVIS asyncio voice gateway (WebSocket on loopback:8765)
After=network-online.target jarvis-agent.service
Wants=network-online.target
Requires=jarvis-agent.service

StartLimitIntervalSec=300
StartLimitBurst=10
OnFailure=jarvis-alert@%n.service

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=60
Restart=always
RestartSec=5
TimeoutStartSec=60
TimeoutStopSec=30
KillMode=control-group
SendSIGKILL=yes

User=jarvis
Group=jarvis
UMask=0027
WorkingDirectory=/opt/jarvis

Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
# 只绑 loopback：公网/内网都不可直连，必须经 tailscale serve 或 Caddy
Environment=JARVIS_GATEWAY_BIND=127.0.0.1
Environment=JARVIS_GATEWAY_PORT=8765
Environment=JARVIS_WS_TOKEN_FILE=%d/jarvis-ws-token

ExecStart=/opt/jarvis/.venv/bin/python -m jarvis.gateway

StateDirectory=jarvis
RuntimeDirectory=jarvis
RuntimeDirectoryMode=0750

LoadCredentialEncrypted=jarvis-ws-token

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectProc=invisible
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
SystemCallArchitectures=native

MemoryHigh=768M
MemoryMax=1G
TasksMax=256

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/jarvis.target —— 一起起停
[Unit]
Description=JARVIS stack
Wants=jarvis-agent.service jarvis-gateway.service
After=jarvis-agent.service jarvis-gateway.service

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/jarvis-alert@.service
# OnFailure=jarvis-alert@%n.service 传入的 %i 形如 jarvis-agent.service
[Unit]
Description=Send failure alert for %i
[Service]
Type=oneshot
ExecStart=/usr/local/bin/jarvis-notify "JARVIS unit FAILED: %i"
```

### 2.4 网关入口：二选一

**方案 A — 全部设备在 tailnet（推荐，零额外守护进程）**
```bash
# 网关已只绑 127.0.0.1:8765
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status
# 设备连 wss://<host>.<tailnet>.ts.net/  —— 证书自动签发与续期
```

**方案 B — 需要公网/域名时用 Caddy**
```caddyfile
# /etc/caddy/Caddyfile
jarvis.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8765 {
        transport http {
            keepalive 60m
            keepalive_interval 10s
        }
    }
    # Web 面板：仅允许 loopback 来源（Caddy 自身在 443 上对外）
    @panel path /panel/*
    handle @panel {
        @notlocal not remote_ip 127.0.0.1 ::1
        respond @notlocal "forbidden" 403
        reverse_proxy 127.0.0.1:8766
    }
}
```
Caddy 自动识别 WebSocket 升级，不需要手写 `Upgrade`/`Connection`。

**防火墙（配合 A/B 都要做）：**
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0                 # tailnet 全通（或细化到 443）
sudo ufw allow 41641/udp                        # WireGuard，提升直连成功率
sudo ufw allow 443/tcp                          # 仅方案 B 需要
# 不要 ufw allow 22 —— SSH 只走 tailscale0
sudo ufw enable
```

### 2.5 备份脚本 + timer

```bash
#!/usr/bin/env bash
# /usr/local/bin/jarvis-backup.sh
set -euo pipefail

DB=/var/lib/jarvis/memory.db
STAGING=/var/backups/jarvis/staging
HC_URL="${HEALTHCHECK_URL:?set HEALTHCHECK_URL}"

mkdir -p "$STAGING"
rm -f "$STAGING"/memory.db

# 1) 在线一致快照（不用 cp）
sqlite3 "$DB" ".backup '$STAGING/memory.db'"

# 2) 送异地之前先验完整性——这一步不能省
RESULT=$(sqlite3 "$STAGING/memory.db" "PRAGMA integrity_check;")
if [ "$RESULT" != "ok" ]; then
  echo "INTEGRITY FAIL: $RESULT" >&2
  curl -fsS -m 10 -o /dev/null "$HC_URL/fail" || true
  exit 1
fi

# 3) 配置与非 DB 资产一并入仓
restic backup \
  "$STAGING" \
  /etc/jarvis \
  /etc/systemd/system/jarvis-agent.service \
  /etc/systemd/system/jarvis-gateway.service \
  /etc/systemd/system/jarvis-backup.service \
  /etc/systemd/system/jarvis-backup.timer \
  --tag jarvis --host "$(hostname)"

# 4) 保留策略（prune 每周单独跑，见 timer）
restic forget --tag jarvis --host "$(hostname)" \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-last 3

# 5) 死信：成功也要 ping，否则"脚本没跑"无法被发现
curl -fsS -m 10 -o /dev/null "$HC_URL" || true
echo "backup ok $(date -Is)"
```

```ini
# /etc/systemd/system/jarvis-backup.service
[Unit]
Description=JARVIS SQLite + config backup to restic
After=network-online.target
Wants=network-online.target
OnFailure=jarvis-alert@%n.service

[Service]
Type=oneshot
EnvironmentFile=/etc/jarvis/backup.env       # RESTIC_REPOSITORY / RESTIC_PASSWORD / HEALTHCHECK_URL
ExecStart=/usr/local/bin/jarvis-backup.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/backups/jarvis
PrivateTmp=yes
```

```ini
# /etc/systemd/system/jarvis-backup.timer
[Unit]
Description=Run JARVIS backup every 6h

[Timer]
OnCalendar=*-*-* 00/6:15:00
RandomizedDelaySec=300
# 关机错过的窗口开机后补跑
Persistent=true

[Install]
WantedBy=timers.target
```
```ini
# /etc/systemd/system/jarvis-prune.service（每周一次，与备份解耦）
[Unit]
Description=JARVIS restic prune + subset check
[Service]
Type=oneshot
EnvironmentFile=/etc/jarvis/backup.env
ExecStart=/usr/bin/restic prune --max-unused 5%
ExecStart=/usr/bin/restic check --read-data-subset=1G/10
Nice=19
IOSchedulingClass=idle
```
```ini
# /etc/systemd/system/jarvis-prune.timer
[Unit]
Description=Weekly JARVIS restic maintenance
[Timer]
OnCalendar=Sun *-*-* 04:30:00
Persistent=true
[Install]
WantedBy=timers.target
```

### 2.6 探针脚本（五个探针合一）

```bash
#!/usr/bin/env bash
# /usr/local/bin/jarvis-probe.sh —— 由 timer 每 2 分钟跑
set -uo pipefail
FAIL=0
note() { echo "[FAIL] $*" >&2; FAIL=1; }

# 1) 黑盒 WS 探活：真握手 + 一帧，不是 nc -z
if ! /opt/jarvis/.venv/bin/python - <<'PY'
import asyncio, json, os, sys
import websockets
async def main():
    tok = open(os.environ["JARVIS_WS_TOKEN_FILE"]).read().strip()
    async with websockets.connect(
        "ws://127.0.0.1:8765/", open_timeout=5,
        additional_headers={"Authorization": f"Bearer {tok}"},
        ping_interval=20, ping_timeout=10) as ws:
        await ws.send(json.dumps({"type": "ping"}))
        await asyncio.wait_for(ws.recv(), timeout=10)
asyncio.run(main())
PY
then note "websocket handshake/echo failed"; fi

# 2) 心跳文件 mtime：抓"进程在但循环卡死"
HB=/run/jarvis/heartbeat
if [ ! -f "$HB" ]; then
  note "heartbeat file missing"
else
  AGE=$(( $(date +%s) - $(stat -c %Y "$HB") ))
  [ "$AGE" -gt 300 ] && note "heartbeat stale: ${AGE}s"
fi

# 3) 重启循环：is-active 会骗人，NRestarts 不会
NR=$(systemctl show -p NRestarts --value jarvis-agent.service)
UP=$(systemctl show -p ActiveEnterTimestampMonotonic --value jarvis-agent.service)
[ "${NR:-0}" -gt 3 ] && note "agent restarted ${NR} times since boot"

# 4) 磁盘 + RSS
DISK=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${DISK:-0}" -ge 85 ] && note "disk ${DISK}% used"
for u in jarvis-agent jarvis-gateway; do
  RSS=$(systemctl show -p MemoryCurrent --value "$u.service")
  [ "${RSS:-0}" != "[not set]" ] && [ "${RSS:-0}" -gt $((900*1024*1024)) ] \
    && note "$u RSS $((RSS/1024/1024))M"
done

# 5) 告警链路心跳：无论成败都 ping，否则"探针自己死了"没人知道
curl -fsS -m 10 -o /dev/null "${PROBE_HC_URL:?}" || true

exit "$FAIL"
```

```ini
# /etc/systemd/system/jarvis-probe.service
[Unit]
Description=JARVIS blackbox probe
[Service]
Type=oneshot
EnvironmentFile=/etc/jarvis/probe.env
ExecStart=/usr/local/bin/jarvis-probe.sh
```
```ini
# /etc/systemd/system/jarvis-probe.timer
[Unit]
Description=Run JARVIS probe every 2 minutes
[Timer]
OnBootSec=90
OnUnitActiveSec=2min
AccuracySec=10s
[Install]
WantedBy=timers.target
```
> 探针失败时的通知由 systemd 的 `OnFailure=` 承担（在 `jarvis-probe.service` 上加一行 `OnFailure=jarvis-alert@%n.service`），或直接在脚本里调 `jarvis-notify`。

### 2.7 告警清单（阈值即代码）

| 级别 | 条件 | 动作 |
|---|---|---|
| P1 | WS 握手失败连续 3 次（6 分钟） | 立即推送；查 `journalctl -u jarvis-gateway -n 200` |
| P1 | 心跳文件 mtime > 5 分钟 | 立即推送；多半是事件循环死锁或 DB 锁 |
| P1 | 单元进入 `failed` / `start-limit-hit` | `OnFailure=` 即时触发；**必须人工介入**，`Restart=` 已放弃 |
| P2 | `NRestarts` 24h 内 > 5 | 推送；说明在崩溃循环但被 `is-active` 掩盖 |
| P2 | 备份超过 12h 无成功记录 | 死信告警 |
| P2 | 磁盘 > 85% | 推送；先清 journal（`journalctl --vacuum-size=500M`）再查快照 |
| P3 | RSS > `MemoryHigh` 的 90% | 记录，连续 3 次才推送 |
| P3 | 证书剩余 < 14 天 | 记录（`tailscale serve`/Caddy 自动续期，异常才需要看） |

### 2.8 上线与验证命令

```bash
# 语法与沙箱评分（CI 闸门：--threshold 设 5，超过就失败）
sudo systemd-analyze verify /etc/systemd/system/jarvis-*.service
sudo systemd-analyze security --offline=true --threshold=5 jarvis-agent.service

# 启动顺序：先 agent（gateway Requires= 它）
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis.target
sudo systemctl enable --now jarvis-backup.timer jarvis-prune.timer jarvis-probe.timer

# 验证 ready 与 watchdog 真的在跳
sudo systemctl show jarvis-agent -p Type -p WatchdogUSec -p WatchdogTimestamp -p NRestarts -p StatusText
# WatchdogTimestamp 应随时间推进；StatusText 是你发的 STATUS= 文本

# 验证凭据真的没进环境变量
sudo systemctl show jarvis-agent -p Environment | grep -i key || echo "OK: no key in Environment="
sudo tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value jarvis-agent)/environ | grep -i 'sk-' \
  && echo "LEAK: key in /proc/environ" || echo "OK: no key in /proc/environ"

# 验证只绑 loopback
sudo ss -lntp | grep 8765      # 必须 127.0.0.1:8765，不能 0.0.0.0:8765

# 验证重启真的能恢复（手工演练，别只信配置）
sudo systemctl kill -s SIGKILL jarvis-agent && sleep 15 && systemctl is-active jarvis-agent
sudo systemctl stop jarvis-gateway && sudo systemctl start jarvis-gateway

# 演练恢复：从异地仓库拉一份并校验
restic restore latest --target /tmp/drill --include /var/backups/jarvis/staging/memory.db
sqlite3 /tmp/drill/var/backups/jarvis/staging/memory.db "PRAGMA integrity_check;"
```

---

## ③ 迁移检查表

### 阶段 0：准备（迁移前，Mac 上做）

- [ ] 盘点所有 `~` / `Path.home()` / 绝对路径硬编码，改为读 `JARVIS_*_DIR` 环境变量
- [ ] 盘点所有"直接播放音频/直接录音"的代码路径，改为文件渲染或设备侧采集
- [ ] 确认 `memory.db` 是 WAL 模式（`PRAGMA journal_mode;` → `wal`）
- [ ] 确认代码里设了 `PRAGMA busy_timeout = 5000`（备份/Litestream 抢锁必需）
- [ ] `uv lock` 生成 `uv.lock` 并提交
- [ ] 记录当前 SQLite 关键表行数，作为迁移后比对基线
- [ ] 生成新的设备 token（**不要**沿用 Mac 上那个），规划每设备一枚
- [ ] 决定入口方案：A（tailscale serve）还是 B（Caddy + 域名）

### 阶段 1：主机基线

- [ ] 创建非 root 用户，SSH 只用密钥、关密码登录
- [ ] 装 Tailscale，`tailscale up`，`tailscale ip -4` 记下 `100.x` 地址
- [ ] `ufw` 默认拒绝入站；**不放行 22**，SSH 只走 `tailscale0`
- [ ] 在 Tailscale 管理台**关闭该机器的 key 过期**（否则 180 天后悄悄掉线）
- [ ] `timedatectl set-timezone Asia/Shanghai`；确认 `System clock synchronized: yes`
- [ ] 开启 `unattended-upgrades`（安全补丁自动装）
- [ ] 记录云厂商的 **rescue console / KVM 入口**（Tailscale 挂了时的唯一退路）

### 阶段 2：文件与权限

- [ ] `/opt/jarvis` 代码就位，`uv sync --locked --no-editable`
- [ ] 用 `jarvis` 用户跑一次 `python -c "import jarvis"` 确认 venv 可用
- [ ] `/var/lib/jarvis`、`/etc/jarvis`、`/run/jarvis`、`/var/backups/jarvis/staging` 属主与 mode 符合 2.1
- [ ] 迁移 `memory.db`：**先停服务**，`sqlite3 .backup` 到目标，再 `chown jarvis:jarvis`，`chmod 0640`
- [ ] 确认没有残留的 `memory.db-wal` / `-shm` 被一起拷过去（拷了也无害，但属主必须对）
- [ ] 所有密钥文件 `0600`，且**不在** `/opt/jarvis` 或任何 git 工作树里

### 阶段 3：密钥升级

- [ ] `systemd-creds setup`（云主机无 TPM 时确保 host key 生成）
- [ ] 模型 key 加密进 `/etc/credstore.encrypted/jarvis-model-api-key.cred`
- [ ] 设备 token 加密进 `/etc/credstore.encrypted/jarvis-ws-token.cred`
- [ ] 应用改为读 `$JARVIS_MODEL_KEY_FILE` 指向的文件，**不再读环境变量里的值**
- [ ] `systemctl show -p Environment` 与 `/proc/<pid>/environ` 双重确认无明文 key
- [ ] `/var/lib/systemd/credential.secret` 已纳入离线备份（丢了这些凭据全废）

### 阶段 4：服务上线

- [ ] unit 文件写入 `/etc/systemd/system/`，`systemd-analyze verify` 无输出
- [ ] `systemd-analyze security --threshold=5` 通过
- [ ] `StartLimitIntervalSec` / `StartLimitBurst` 确认在 **`[Unit]`** 段
- [ ] `KillMode=control-group`（不是 `mixed`），`SendSIGKILL=yes`
- [ ] 应用实现了 `READY=1`（在 bind + DB 就绪之后才发）
- [ ] 应用实现了 `WATCHDOG=1`，且**由事件循环内的 task 发出**
- [ ] `systemctl show ... WatchdogTimestamp` 随时间推进
- [ ] `ss -lntp` 确认 8765 只绑 `127.0.0.1`
- [ ] Web 面板端口只绑 `127.0.0.1`，且只允许 loopback 来源
- [ ] 入口打通：设备用 `wss://` 连上，`ping`/`echo` 往返正常
- [ ] 空闲 10 分钟不断连（验证反代超时已调）
- [ ] 手工 `kill -9` 主进程，15 秒内自动恢复且能正常响应
- [ ] 手工 stop/start 网关，无孤儿进程残留（`systemd-cgls jarvis-gateway`）

### 阶段 5：备份与监控

- [ ] restic 仓库初始化，密码与仓库地址放 `/etc/jarvis/backup.env`（0600）
- [ ] 仓库开 **append-only / Object Lock**（防勒索删历史）
- [ ] `jarvis-backup.sh` 手工跑通一次，`restic snapshots` 能看到
- [ ] **完整恢复演练做过一次**，记录 time-to-restore 与 `integrity_check` 输出
- [ ] `jarvis-backup.timer` / `jarvis-prune.timer` / `jarvis-probe.timer` 已 enable
- [ ] 死信 URL 配好；**故意让备份失败一次**，确认告警真的收到
- [ ] 五个探针逐个"人为破坏—确认告警—恢复"各演练一次
- [ ] 告警能到你**手机上**（不是只到日志里）

### 阶段 6：收尾

- [ ] 连续观察 72 小时：`NRestarts` 应为 0 或个位数，`MemoryCurrent` 应平稳不爬升
- [ ] `journalctl --disk-usage` 在配额内；`df` 使用率稳定
- [ ] 把上面所有"怎么恢复/怎么排障"写进 `RUNBOOK.md`，含救援控制台入口
- [ ] Mac 上的 launchd 服务 `launchctl bootout` 并**确认不再自动起来**（双跑会抢同一个 token 和 DB 写入）
- [ ] 旧 Mac 上的密钥文件 shred 掉

---

## ④ 证据等级

**分级定义**
- **A｜一手官方文档**：项目自己的 man page / 官方 docs / 官方设计文档。可直接作为配置依据。
- **B｜官方文档的二手转述或厂商工程博客**：结论正确但细节可能滞后于版本，配置前需对照 A 复核。
- **C｜社区实践 / 论坛 / 单一博客**：作为"存在这个问题"的证据，不作为"应该这样配"的唯一依据。
- **D｜本方案的工程判断**：基于上述证据 + 本系统具体约束做出的取舍，不是文档里的原话。

| 结论 | 等级 | 依据 |
|---|---|---|
| `StartLimitIntervalSec`/`StartLimitBurst` 属 `[Unit]`，放 `[Service]` 静默忽略 | **A**（行为）+ **C**（该 issue 的具体复现） | systemd v230 变更；freenet issue #4551；hvr.biz 限速数学 |
| `KillMode=mixed` 与 `ExecStopPost=` 同用会漏杀 | **A** | systemd.kill man page + systemd issue #14566 |
| 孤儿子进程致 `Failed to kill control group`、服务停 failed | **C** | Hermes issue #1617（本项目同类栈的真实报告） |
| `Type=notify` 不发 `READY=1` 卡 activating 至 `TimeoutStartSec` | **A** | systemd.service man page |
| `WatchdogSec` 需半间隔 ping；`NotifyAccess` 默认 `main` | **A** | systemd.service man page |
| watchdog ping 必须在事件循环内发（线程 ping 会掩盖死锁） | **D** | 机制推导 + oneuptime 的线程示例反证 |
| `ProtectSystem=strict` / `ProtectHome=` 精确语义 | **A** | systemd.exec man page |
| `DynamicUser=` 隐式开启 6 项加固 | **A** | systemd.exec man page（Ubuntu manpages 版明确列出） |
| 本方案不用 `DynamicUser`，改静态用户 + 显式加固 | **D** | 稳定性/备份/硬编码路径三项约束的取舍 |
| `LoadCredential` 不进 `/proc/environ`、不向下进程树传播、不可交换内存 | **A** | systemd CREDENTIALS.md 设计文档 |
| `LoadCredentialEncrypted` 解密失败即单元启动失败 | **A** | systemd.exec + systemd-creds 文档 |
| `/var/lib/systemd/credential.secret` 丢失 = host-bound 凭据全废 | **A** | CREDENTIALS.md |
| 单机上 root 可读一切，加密只防静态泄露 | **D**（判断）+ **C**（同结论的实践文） | systhoughts 的威胁模型排序 |
| 上游 key 不出网关、虚拟 key 可撤销带预算 | **A**（LiteLLM 官方能力）+ **D**（本场景是否值得） | LiteLLM virtual keys 文档 |
| Nginx `Upgrade` 是 hop-by-hop 头、必须显式透传 | **A** | nginx.org WebSocket 文档 |
| Nginx 默认 60s 无数据即断，需 `proxy_read_timeout` 或后端 ping | **A** | nginx.org 同页 |
| 漏 `proxy_http_version 1.1` / 用 `$http_connection` 是两大坑 | **B** | stackharbor（与 A 一致，细节来自实践） |
| Caddy 自动处理 WS 升级与自动 HTTPS | **A** | Caddy reverse_proxy 官方文档 |
| `tailscale serve` 自动签发并续期证书；后端仅支持 `http://127.0.0.1` | **A** | Tailscale CLI serve 文档 |
| `tailscale cert` 手工取证书需自行续期 | **A** | Tailscale HTTPS 文档 |
| SSH 只走 `tailscale0`、关公网 22 后可省 fail2ban | **C** | 多篇实践文一致（wihan.dev 等） |
| 活库不能用 `cp`/`tar` 备份（WAL 下会漏/torn） | **A** | Litestream 官方 cron 文档 |
| `.backup` 在线安全；`VACUUM INTO` 单事务快照 | **A** | SQLite/Litestream 官方 |
| 送异地前必须 `PRAGMA integrity_check` | **A**（机制）+ **B**（作为流程建议） | bigiron 指南 + Litestream 官方均提及 |
| Litestream 0.5 用 `replica`（单数）、`snapshot` 为全局块、retention 默认 24h | **B** | ssdnodes 指南（对照 0.5 版行为；建议装完用 `litestream databases` 自验） |
| Litestream 需 `busy_timeout=5000`、仅 WAL、禁 `VACUUM`、禁双实例 | **A** | litestream.io/tips 官方 |
| restic `forget` 只标记、`prune` 才回收；`--keep-*` 按自然时间边界 | **A** | restic 官方 060_forget.rst |
| 备份必须有死信告警，否则静默失败 | **A**（机制）+ **B**（实践） | healthchecks 模式 + Julia Evans 的 SQLite 生产经验 |
| journald 只删归档文件、超配额后可能仍超 | **A** | journald.conf man page |
| journald 限速按服务独立计数，默认 30s/10000 | **A** | journald.conf man page |
| 单机先用 cron+脚本+死信，别上 Prometheus | **D** | 运维成本与收益权衡；alertmanager 自身需被监控是 A 级事实 |
| Watchdog 告警 + 外部心跳，且每集群独立 URL | **A**（kube-prometheus 模式）+ **B**（实现细节） | hyperping 实测文 |
| `is-active` 会掩盖重启循环，要看 `NRestarts` | **A**（`systemctl show` 属性）+ **D**（作为监控项） | systemd 属性文档 + 实践 |
| launchd 键到 systemd 的映射 | **A**（launchd.plist man page）+ **B**（对照表） | manpagez launchd.plist + 对照文 |
| 服务器用系统单元而非 `--user` | **D** | linger/命名空间差异的取舍 |
| 云主机无 `/dev/snd`，`snd-dummy` 通常不可用 | **B** | superuser 讨论（内核模块可用性依发行版） |
| 时区默认 UTC，影响定时与 token 过期 | **A**（事实）+ **D**（作为迁移项） | `timedatectl` 行为 |
| `systemd-analyze security` 可作 CI 闸门 | **A** | systemd-analyze man page |

**已知未验证项（部署时需实测）**
1. `SystemCallFilter=@system-service` 是否与 JARVIS 的某个 C 扩展/子进程冲突——可能报 EPERM，需按注释逐项放宽。
2. `MemoryHigh=1500M` / `MemoryMax=2G` 是否够用——取决于模型上下文与并发设备数，应先观察一周再定。
3. Litestream 0.5 的确切配置字段（`replica` vs `replicas`）建议装完立刻 `litestream databases -config /etc/litestream.yml` 自验，别只信本文。
4. 若走方案 B，Caddy 的 `keepalive 60m` 与 iOS 后台挂起行为的实际交互，需要真机连 10 分钟以上验证。
