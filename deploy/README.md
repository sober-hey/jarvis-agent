# 部署（Linux 生产目标）

状态：**草案，未在真实 Linux 主机上验收**。当前生产目标是阿里云 ECS / Alibaba Cloud Linux 4 x86_64。
依据见 [../docs/research/production_deploy_research.md](../docs/research/production_deploy_research.md)。

## 目录约定

| 路径 | 内容 |
|---|---|
| `/opt/jarvis` | 代码（只读，`ProtectSystem=strict` 下不可写） |
| `/opt/jarvis/.venv` | 依赖（部署时 `uv sync --locked --no-editable`，不在启动时联网） |
| `/var/lib/jarvis` | 运行数据（记忆库、语音片段） |
| `/var/log/jarvis` | 日志 |
| `/etc/jarvis` | 配置与凭据（`LoadCredential` 源文件，0400 root） |

## 安装步骤

```bash
# 1. 系统用户与目录
useradd --system --home /var/lib/jarvis --shell /usr/sbin/nologin jarvis
install -d -o jarvis -g jarvis -m 0750 /var/lib/jarvis /var/log/jarvis
install -d -m 0750 /etc/jarvis

# 2. 代码与依赖（不要在 unit 里同步依赖，避免重启依赖公网）
install -d /opt/jarvis && rsync -a --exclude evidence/ ./ /opt/jarvis/
cd /opt/jarvis && uv sync --locked --no-editable --extra runtime

# 3. 凭据（root 拥有 0400，systemd 运行时复制进 tmpfs 私有目录）
install -m 0400 -o root -g root api.key            /etc/jarvis/api.key
install -m 0400 -o root -g root dashscope.key      /etc/jarvis/dashscope.key
install -m 0400 -o root -g root volcengine.key     /etc/jarvis/volcengine.key
install -m 0400 -o root -g root voice-gateway.token /etc/jarvis/voice-gateway.token

# 4. 单元
install -m 0644 deploy/systemd/jarvis-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now jarvis-daemon.service jarvis-voice-gateway.service
```

## 几个不能改错的点（来自调研，均有出处）

1. `StartLimitIntervalSec` / `StartLimitBurst` 必须写在 `[Unit]`，写在 `[Service]` 会被静默忽略，崩溃循环拦不住。
2. `KillMode=control-group`，不要用 `mixed`：混合模式下主进程按时退出时可能漏杀子进程，残留子进程会让单元进 failed，`Restart=` 不生效。
3. `WatchdogSec` 的心跳必须由事件循环内的任务发。用独立线程定时 ping，事件循环卡死时线程照样在 ping，watchdog 等于没有。
4. 不用 `DynamicUser=yes`：UID 每次启动可能变，和「稳定属主的 SQLite + 备份去重」冲突；`Path.home()` 也会指向 `/`。改用静态 `jarvis` 用户，并把沙箱项逐条手写。
5. `LoadCredential` 而不是 `Environment=`：环境变量会出现在 `/proc/<pid>/environ`，凭据目录不会向下进程树传播。

## 网络入口

网关目前是明文 `ws://` + 共享 Bearer，**只能用于内网或 Tailscale**：

- 首选：设备侧走 Tailscale，网关只监听回环 + Tailscale 地址。
- 若要公网：前面加 Caddy/Nginx 终止 TLS（`wss://`），并对客户端做证书或 token 校验；不要把 8765 直接暴露到公网。
- Web 面板仍只监听回环，需要远程访问就通过 SSH 隧道。

## 备份与恢复

```bash
# 每日热备份（在线一致性快照 + integrity_check + 行数比对 + sha256）
JARVIS_BACKUP_DIR=/var/backups/jarvis sh scripts/backup_mind_db.sh
```

异地：把备份目录用 restic 推到对象存储。恢复演练的最低标准：

1. 新机装好代码与依赖；
2. 从备份恢复 `mind.db`，`PRAGMA integrity_check` 通过；
3. `SELECT count(*) FROM entries` 与备份时记录一致；
4. 启动守护进程，能读到最近 20 条记忆；
5. 上述四步写进恢复记录，才算"备份可用"。

## 令牌轮换（撤权）

网关用共享 Bearer 令牌，没有逐设备吊销机制；令牌一旦外泄，唯一手段是整体轮换：

```bash
scripts/rotate_gateway_token.sh --dry-run   # 先看要做什么
scripts/rotate_gateway_token.sh             # 生成新令牌、原子落盘、重启网关
```

脚本只做「生成—落盘—重启—验证监听」，不代填设备：轮换后要在 Mac/iPhone/Watch
设置页各填一次新令牌。旧令牌随即失效（本机网关实测：无 token 与错误 token 均返回 401）。

## 健康检查

```bash
python3 scripts/healthcheck.py          # 人读
python3 scripts/healthcheck.py --json   # 告警系统读，异常退出码 1
```

检查项：网关端口、近 30 分钟是否有设备 hello、守护进程心跳、记忆库与 WAL 体积、磁盘余量、三个进程 RSS。
建议挂 systemd timer 每 5 分钟跑一次，异常时通过 iMessage 告警。
